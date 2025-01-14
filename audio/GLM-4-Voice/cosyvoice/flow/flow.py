# Copyright (c) 2024 Alibaba Inc (authors: Xiang Lyu, Zhihao Du)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import random
from typing import Dict, Optional
import torch
import torch.nn as nn
from torch.nn import functional as F
from omegaconf import DictConfig
from cosyvoice.utils.mask import make_pad_mask


class MaskedDiffWithXvec(torch.nn.Module):
    def __init__(self,
                 input_size: int = 512,
                 output_size: int = 80,
                 spk_embed_dim: int = 192,
                 output_type: str = "mel",
                 vocab_size: int = 4096,
                 input_frame_rate: int = 50,
                 only_mask_loss: bool = True,
                 encoder: torch.nn.Module = None,
                 length_regulator: torch.nn.Module = None,
                 decoder: torch.nn.Module = None,
                 decoder_conf: Dict = {'in_channels': 240, 'out_channel': 80, 'spk_emb_dim': 80, 'n_spks': 1, 'cfm_params': DictConfig({'sigma_min': 1e-06, 'solver': 'euler', 't_scheduler': 'cosine', 'training_cfg_rate': 0.2, 'inference_cfg_rate': 0.7, 'reg_loss_type': 'l1'}), 'decoder_params': {'channels': [256, 256], 'dropout': 0.0, 'attention_head_dim': 64, 'n_blocks': 4, 'num_mid_blocks': 12, 'num_heads': 8, 'act_fn': 'gelu'}},
                 mel_feat_conf: Dict = {'n_fft': 1024, 'num_mels': 80, 'sampling_rate': 22050, 'hop_size': 256, 'win_size': 1024, 'fmin': 0, 'fmax': 8000}):
        super().__init__()
        self.input_size = input_size
        self.output_size = output_size
        self.decoder_conf = decoder_conf
        self.mel_feat_conf = mel_feat_conf
        self.vocab_size = vocab_size
        self.output_type = output_type
        self.input_frame_rate = input_frame_rate
        logging.info(f"input frame rate={self.input_frame_rate}")
        self.input_embedding = nn.Embedding(vocab_size, input_size)
        self.spk_embed_affine_layer = torch.nn.Linear(spk_embed_dim, output_size)
        self.encoder = encoder
        self.encoder_proj = torch.nn.Linear(self.encoder.output_size(), output_size)
        self.decoder = decoder
        self.length_regulator = length_regulator
        self.only_mask_loss = only_mask_loss

    def forward(
            self,
            batch: dict,
            device: torch.device,
    ) -> Dict[str, Optional[torch.Tensor]]:
        token = batch['speech_token'].to(device)
        token_len = batch['speech_token_len'].to(device)
        feat = batch['speech_feat'].to(device)
        feat_len = batch['speech_feat_len'].to(device)
        embedding = batch['embedding'].to(device)

        # xvec projection
        embedding = F.normalize(embedding, dim=1)
        embedding = self.spk_embed_affine_layer(embedding)
        # embedding=None

        # concat text and prompt_text
        mask = (~make_pad_mask(token_len)).float().unsqueeze(-1).to(device)
        # print(token.max(),self.input_embedding)
        token = self.input_embedding(torch.clamp(token, min=0)) * mask


        # text encode
        h, h_lengths = self.encoder(token, token_len)
        h = self.encoder_proj(h)
        h, h_lengths = self.length_regulator(h, feat_len)

        # get conditions
        conds = torch.zeros(feat.shape, device=token.device)
        for i, j in enumerate(feat_len):
            if random.random() < 0.5:
                continue
            index = random.randint(0, int(0.8 * j))
            conds[i, :index] = feat[i, :index]
        conds = conds.transpose(1, 2)

        mask = (~make_pad_mask(feat_len)).to(h)
        feat = F.interpolate(feat.unsqueeze(dim=1), size=h.shape[1:], mode="nearest").squeeze(dim=1)
        loss, _ = self.decoder.compute_loss(
            feat.transpose(1, 2).contiguous(),
            mask.unsqueeze(1),
            h.transpose(1, 2).contiguous(),
            embedding,
            cond=conds
        )
        return {'loss': loss}

    @torch.inference_mode()
    def inference(self,
                  token,
                  token_len,
                  prompt_token,
                  prompt_token_len,
                  prompt_feat,
                  prompt_feat_len,
                  embedding):
        assert token.shape[0] == 1
        # xvec projection
        embedding = F.normalize(embedding, dim=1)  # [1, 192]
        embedding = self.spk_embed_affine_layer(embedding)  # [1, 80]

        # concat text and prompt_text
        token, token_len = torch.concat([prompt_token, token], dim=1), prompt_token_len + token_len  # 将之前预测出的token和当前预测出的token拼接，shape如[1, 25]和[25]
        mask = (~make_pad_mask(token_len)).float().unsqueeze(-1).to(embedding)  # [1, 25, 1]
        token = self.input_embedding(torch.clamp(token, min=0)) * mask  # [1, 25, 512]

        # text encode
        h, h_lengths = self.encoder(token, token_len)  # [1, 25, 512]和[1, 1, 25]
        h = self.encoder_proj(h)  # [1, 25, 80]
        feat_len = (token_len / self.input_frame_rate * 22050 / 256).int()  # 如shape[1]，当前speech tokens序列的长度为25，计算后的mel谱图长度为172
        h, h_lengths = self.length_regulator(h, feat_len)  # [1, 172, 80]，通过长度调节器将h的长度调节为172

        # get conditions
        conds = torch.zeros([1, feat_len.max().item(), self.output_size], device=token.device)  # [1, 172, 80]
        if prompt_feat.shape[1] != 0:  # 如果prompt_feat不为空，则将prompt_feat填充到conds中
            for i, j in enumerate(prompt_feat_len):
                conds[i, :j] = prompt_feat[i]
        conds = conds.transpose(1, 2)  # [1, 80, 172]

        mask = (~make_pad_mask(feat_len)).to(h)  # [1, 172]
        feat = self.decoder(
            mu=h.transpose(1, 2).contiguous(),
            mask=mask.unsqueeze(1),
            spks=embedding,
            cond=conds,
            n_timesteps=10
        )  # [1, 80, 172]，通过flow decoder预测出mel谱图
        if prompt_feat.shape[1] != 0:  # 如果prompt_feat不为空，则将prompt_feat从feat中截取出来
            feat = feat[:, :, prompt_feat.shape[1]:]
        return feat