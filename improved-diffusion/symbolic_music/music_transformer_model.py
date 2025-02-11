import math

from symbolic_music.music_transformer_by_rpr import create_music_transformer_encoder_by_config
from transformers import AutoConfig
import torch
import torch.nn as nn
from improved_diffusion.nn import (
    SiLU,
    linear,
    timestep_embedding,
)


# PositionalEncoding
# Taken from https://pytorch.org/tutorials/beginner/transformer_tutorial.html
class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class MusicTransformerModel(nn.Module):
    def __init__(
        self,
        in_channels,  # embedding size for the notes  (channels of input tensor)   e.g. 16 / 32 / 128
        model_channels,  # 128, the channel count of the model
        out_channels,  # output channels (embedding size) = in_channels (since discrete data)
        dropout=0,  # dropout rate
        config_name='bert-base-uncased',
        vocab_size=None,  # size of the vocabulary, e.g. 218 for REMI
        experiment_mode='lm',  # lm or conditional_gen
    ):
        super().__init__()

        # load bert config
        config = AutoConfig.from_pretrained(config_name)
        config.hidden_dropout_prob = dropout
        # use music transformer recommended
        # config.intermediate_size = 1024

        self.in_channels = in_channels
        self.model_channels = model_channels
        self.dropout = dropout
        self.max_period = 2048

        # embedding layer  shape -> [*shape, in_channels]
        self.word_embedding = nn.Embedding(vocab_size, self.in_channels)
        # language model head   in_channels -> vocab_size
        self.lm_head = nn.Linear(self.in_channels, vocab_size)
        with torch.no_grad():
            self.lm_head.weight = self.word_embedding.weight

        if experiment_mode == 'conditional_gen':
            self.conditional_gen = True
            self.encoder_emb = nn.Embedding(vocab_size, config.hidden_size)
            self.encoder = create_music_transformer_encoder_by_config(config, max_sequence=self.max_period)
            print(config, 'conditional_gen')
            config.is_decoder = True
            config.add_cross_attention = True
        elif experiment_mode == 'lm':
            self.conditional_gen = False

        time_embed_dim = model_channels * 4
        # time embedding    128 -> 512 -> 768 (bert base hidden size)
        self.time_embed = nn.Sequential(
            linear(model_channels, time_embed_dim),
            SiLU(),
            linear(time_embed_dim, config.hidden_size),
        )
        # in_channels -> 768(hidden_size) -> 768(hidden_size)
        self.input_up_proj = nn.Sequential(
            nn.Linear(in_channels, config.hidden_size),
            nn.Tanh(),
            nn.Linear(config.hidden_size, config.hidden_size)
        )
        print(config)
        # 下述BertLayer * 12
        # 768 ->
        # attention(SelfAttention + output(dense + LayerNorm + drop)) + 放大层dense + output(dense + LayerNorm + drop)
        # -> 768
        self.input_transformers = create_music_transformer_encoder_by_config(config, max_sequence=self.max_period)
        self.positional_encoding = PositionalEncoding(config.hidden_size, self.dropout, self.max_period)
        # self.position_ids
        # self.register_buffer("position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)))
        # position embedding = 512 -> 768
        # self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # 768 -> 768 -> 16
        self.output_down_proj = nn.Sequential(
            nn.Linear(config.hidden_size, config.hidden_size),
            nn.Tanh(),
            nn.Linear(config.hidden_size, out_channels)
        )

    def get_embeds(self, input_ids):
        # shape -> [*shape, in_channels]
        return self.word_embedding(input_ids)

    def get_logits(self, hidden_repr):
        # in_channels (~16) -> vocab_size
        return self.lm_head(hidden_repr)

    def forward(self, x, timesteps, src_ids=None, src_mask=None):
        """
        Apply the model to an input batch.

        :param x: an [N x C x ...] Tensor of inputs.
        :param timesteps: a 1-D batch of timesteps.
        :param y: an [N] Tensor of labels, if class-conditional.
        :return: an [N x C x ...] Tensor of outputs.
        """
        #  timesteps  (1,2,3,4...)  ->    sine positional embedding    ->     128 -> 512 -> 768
        emb = self.time_embed(timestep_embedding(timesteps, self.model_channels, max_period=self.max_period))

        if self.conditional_gen:
            assert src_ids is not None
            # print(src_ids.shape, 'source_ids shape')
            src_emb = self.encoder_emb(src_ids)
            # print(src_ids.shape, src_emb.shape)
            encoder_hidden_states = self.encoder(src_emb)
            encoder_attention_mask = src_mask.unsqueeze(1).unsqueeze(1)

        emb_x = self.input_up_proj(x)

        seq_length = x.size(1)
        # print(emb_x.shape, emb.shape, self.position_embeddings)

        # (,768)
        emb_inputs = self.positional_encoding(emb_x) + emb_x + emb.unsqueeze(1).expand(-1, seq_length, -1)
        emb_inputs = self.dropout(self.LayerNorm(emb_inputs))
        if self.conditional_gen:
            # print(emb_inputs.shape, encoder_hidden_states.shape, encoder_attention_mask.shape)
            input_trans_hidden_states = self.input_transformers(emb_inputs,
                                                                encoder_hidden_states=encoder_hidden_states,
                                                                encoder_attention_mask=encoder_attention_mask,
                                                                )
        else:
            # 768 -> 768
            input_trans_hidden_states = self.input_transformers(emb_inputs)
        # (,768) -> (,16)
        h = self.output_down_proj(input_trans_hidden_states)
        h = h.type(x.dtype)
        return h
