import os
import json
import wandb
import itertools
import numpy as np
from time import gmtime, strftime

import soundfile as sf

import torch
import torchaudio
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data.distributed import DistributedSampler
from torch.cuda.amp import autocast, GradScaler
from speechbrain.pretrained import HIFIGAN

# from loader import TextAudioSpeakerLoader, TextAudioSpeakerCollate, TextAudioLoader, TextAudioCollate
from dataset.gaddy_loader import EMGDataset
from losses import dtw_loss
from models.architecture import Model, Vocoder

from utils.data_utils import phoneme_inventory, decollate_tensor, combine_fixed_length
from utils.audio_utils import spectral_normalize_torch
import commons
# from mel_processing import mel_spectrogram_torch, spec_to_mel_torch
# from text.symbols import symbols


class Solver(object):
	def __init__(self, args):
		self.wandb = wandb.init(entity='jjlee0721', project='gaddy', group=args.base_args.group_name, config=args)
		self.global_step = 0
		wandb.run.name = args.base_args.exp_name

	def build_dataset(self, args):
		self.mp_context = torch.multiprocessing.get_context('fork')

		args.train.batch_size = int(args.train.batch_size / args.base_args.ngpus_per_node)
		self.trainset = EMGDataset(args, dev=False, test=False)
		self.validset = EMGDataset(args, dev=True)
		self.train_sampler = DistributedSampler(self.trainset, shuffle=True, rank=args.base_args.gpu)
		self.train_loader = DataLoader(self.trainset, batch_size=args.train.batch_size,
								shuffle=False, num_workers=args.base_args.workers,
								multiprocessing_context=self.mp_context, collate_fn=self.trainset.collate_raw,
								pin_memory=True, sampler=self.train_sampler, drop_last=True,
								worker_init_fn=lambda _: np.random.seed(int(torch.initial_seed())%(2**32-1)))
		self.valid_sampler = DistributedSampler(self.validset, shuffle=True, rank=args.base_args.gpu)
		self.valid_loader = DataLoader(self.validset, batch_size=args.train.batch_size,
								shuffle=False, num_workers=args.base_args.workers,
								multiprocessing_context=self.mp_context, collate_fn=self.validset.collate_raw,
								pin_memory=True, sampler=self.valid_sampler, drop_last=True,
								worker_init_fn=lambda _: np.random.seed(int(torch.initial_seed())%(2**32-1)))
		self.max_iter = len(self.train_loader)

	def build_models(self, args):
		n_phones = len(phoneme_inventory)
		if args.base_args.hubert:
			net_g = Model(args, args.model.hubert_dim, n_phones)
		else:
			net_g = Model(args, self.trainset.num_speech_features, n_phones)
		if args.data.sample_rate == 22050:
			net_v = Vocoder(**args.vocoder)
		elif args.data.sample_rate == 16000 and 'v' in args.base_args.pretrain[0] and 'v' in args.base_args.fixtrain[0]:
			net_v = HIFIGAN.from_hparams(source="speechbrain/tts-hifigan-libritts-16kHz", savedir="/disk1/pretrained/HiFiGAN/tts-hifigan-libritts-16kHz")
			print("Pretrained vocoder model is loaded")

		####################### Distribute Models to Machines #######################
		torch.cuda.set_device(args.base_args.gpu)
		net_g = net_g.to('cuda:{}'.format(args.base_args.gpu))
		net_v = net_v.to('cuda:{}'.format(args.base_args.gpu))
		net_g = DistributedDataParallel(net_g, device_ids=[args.base_args.gpu], output_device=args.base_args.gpu, find_unused_parameters=True)
		# net_v = DistributedDataParallel(net_v, device_ids=[args.base_args.gpu], output_device=args.base_args.gpu, find_unused_parameters=True)

		self.net = {'g':net_g, 'v':net_v}

	def build_losses(self, args):
		pass

	def build_optimizers(self, args):
		optim_g = torch.optim.AdamW(self.net['g'].parameters(), weight_decay=args.train.weight_decay)
		self.optim = {'g':optim_g, 'v':None}
		if 'v' not in args.base_args.fixtrain[0]:
			self.optim['v'] = torch.optim.AdamW(self.net['v'].parameters(), weight_decay=args.train.weight_decay)

	def build_scheduler(self, args, epoch_str):
		scheduler_g = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optim['g'], 'min', 0.5, patience=args.train.learning_rate_patience)
		self.scheduler = {'g':scheduler_g, 'v':None}
		if 'v' not in args.base_args.fixtrain[0]:
			self.scheduler['v'] = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optim['v'], 'min', 0.5, patience=args.train.learning_rate_patience)

	def build_scaler(self, args):
		self.scaler = GradScaler(enabled=args.train.fp16_run)

	def set_lr(self, args, iteration):
		new_lr = (iteration+1)*args.train.learning_rate / args.train.learning_rate_warmup
		if iteration <= args.train.learning_rate_warmup:
			for param_group in self.optim['g'].param_groups:
				param_group['lr'] = new_lr
			if 'v' not in args.base_args.fixtrain[0]:
				for param_group in self.optim['v'].param_groups:
					param_group['lr'] = new_lr

	def train(self, args, epoch):
		seq_len = 200
		self.net['g'].train()
		if 'v' not in args.base_args.fixtrain[0]:
			self.net['v'].train()
		else:
			self.net['v'].eval()

		losses = []
		for batch_idx, (batch) in enumerate(self.train_loader):
			self.optim['g'].zero_grad()
			if 'v' not in args.base_args.fixtrain[0]:
				self.optim['v'].zero_grad()
			self.set_lr(args, batch_idx)

			# X = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['emg']], seq_len)
			X_raw = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['raw_emg']], seq_len*8)
			# sess = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['session_ids']], seq_len)

			pred, phoneme_pred = self.net['g'](X_raw)

			loss, _, _ = dtw_loss(args, pred, phoneme_pred, batch)
			losses.append(loss.item())	

			loss.backward()
			self.optim['g'].step()
			if 'v' not in args.base_args.fixtrain[0]:
				self.optim['v'].step()

			if args.base_args.rank % args.base_args.ngpus_per_node == 0:
				if self.global_step % args.train.log_interval == 0:
					lr = self.optim['g'].param_groups[0]['lr']
					print("\r[Epoch:{:3d}, {:.0f}%, Step:{}] [Loss:{:.5f}] [{}]"
						.format(epoch, 100.*batch_idx/self.max_iter, self.global_step, loss,
						strftime('%Y-%m-%d %H:%M:%S', gmtime())))
					if args.base_args.test != 1:
						wandb_dict = {"loss/train": loss, "learning_rate":lr}
						wandb_dict.update({"epoch": epoch, "global_step": self.global_step})
						self.wandb.log(wandb_dict)
			if args.base_args.test:
				if batch_idx > 5:
					break
			self.global_step += 1
		return np.mean(losses)
		
	def test(self, args, epoch):
		self.net['g'].eval()
		self.net['v'].eval()
		losses = []
		accuracies = []
		phoneme_confusion = np.zeros((len(phoneme_inventory),len(phoneme_inventory)))
		seq_len = 200
		with torch.no_grad():
			for batch_idx, (batch) in enumerate(self.valid_loader):
				# X = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['emg']], seq_len)
				X_raw = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['raw_emg']], seq_len*8)
				# sess = combine_fixed_length([t.cuda(args.base_args.gpu, non_blocking=True) for t in batch['session_ids']], seq_len)
				pred, phoneme_pred = self.net['g'](X_raw)
				loss, phon_acc, phoneme_confusion = dtw_loss(args, pred, phoneme_pred, batch, True, phoneme_confusion)
				losses.append(loss.item())
				accuracies.append(phon_acc)
		if args.base_args.rank % args.base_args.ngpus_per_node == 0:
			if epoch % args.train.save_model_interval == 0:
				# solver.save_audio(args, epoch, solver.validset[0])
				val_loss = np.mean(losses)
				phoneme_acc = np.mean(accuracies)
				print(f'Epoch:{epoch}, Val loss:{val_loss:.4f}, Acc:{phoneme_acc:.2f}')

				if args.base_args.test != 1:
					wandb_dict = {"loss/val": val_loss, "acc/phoneme": phoneme_acc, "val_epoch": epoch, "val_global_step": self.global_step}
					self.wandb.log(wandb_dict)
		return val_loss

	def save_audio(self, args, epoch, datapoint):
		self.net['g'].eval()
		with torch.no_grad():
			# sess = torch.tensor(datapoint['session_ids']).cuda(args.base_args.gpu, non_blocking=True).unsqueeze(0)
			X = torch.tensor(datapoint['emg'], dtype=torch.float32).cuda(args.base_args.gpu, non_blocking=True).unsqueeze(0)
			# X_raw = torch.tensor(datapoint['raw_emg'], dtype=torch.float32).cuda(args.base_args.gpu, non_blocking=True).unsqueeze(0)			

			pred, _ = self.net['g'](X_raw)
			y = pred.squeeze(0)

			y = self.trainset.mfcc_norm.inverse(y.cpu()).cuda(args.base_args.gpu, non_blocking=True)
			audio = self.net['v'](y.transpose(-1, -2)).cpu().numpy()

			# if args.data.sample_rate == 22050:
			# 	audio = self.net['v'](y.transpose(-1, -2)).cpu().numpy()
			# elif args.data.sample_rate == 16000:
			# 	print(y.type())
			# 	audio = self.net['v'].decode_batch(y.transpose(-1, -2))
			# 	audio = audio.cpu().numpy()

			sf.write(os.path.join(args.base_args.base_dir, 'logs/samples', f'epoch_{epoch}_output.wav'), audio.transpose(-1,-2), args.data.sample_rate)
