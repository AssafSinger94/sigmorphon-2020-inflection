import copy
import math
from collections import namedtuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Distribution

from decoder import TransformerDecoder, TransformerDecoderLayer, TransformerDecoderFinalLayer
from dataloader import BOS_IDX, EOS_IDX, PAD_IDX, UNK_IDX, STEP_IDX

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class SinusoidalPositionalEmbedding(nn.Module):
    """This module produces sinusoidal positional embeddings of any length.
    Padding symbols are ignored.
    """
    def __init__(self, embedding_dim, padding_idx, init_size=1024):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.padding_idx = padding_idx
        self.weights = SinusoidalPositionalEmbedding.get_embedding(
            init_size,
            embedding_dim,
            padding_idx,
        )
        self.register_buffer('_float_tensor', torch.FloatTensor(1))

    @staticmethod
    def get_embedding(num_embeddings, embedding_dim, padding_idx=None):
        """Build sinusoidal embeddings.
        This matches the implementation in tensor2tensor, but differs slightly
        from the description in Section 3.5 of "Attention Is All You Need".
        """
        half_dim = embedding_dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.float) * -emb)
        emb = torch.arange(num_embeddings,
                           dtype=torch.float).unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat([torch.sin(emb), torch.cos(emb)],
                        dim=1).view(num_embeddings, -1)
        if embedding_dim % 2 == 1:
            # zero pad
            emb = torch.cat([emb, torch.zeros(num_embeddings, 1)], dim=1)
        if padding_idx is not None:
            emb[padding_idx, :] = 0
        return emb

    def forward(self, input):
        """Input is expected to be of size [bsz x seqlen]."""
        bsz, seq_len = input.shape
        max_pos = self.padding_idx + 1 + seq_len
        if self.weights is None or max_pos > self.weights.size(0):
            # recompute/expand embeddings if needed
            self.weights = SinusoidalPositionalEmbedding.get_embedding(
                max_pos,
                self.embedding_dim,
                self.padding_idx,
            )
        self.weights = self.weights.to(self._float_tensor)

        mask = input.ne(self.padding_idx).long()
        positions = torch.cumsum(mask, dim=0) * mask + self.padding_idx
        return self.weights.index_select(0, positions.view(-1)).view(
            bsz, seq_len, -1).detach()


class TransformerEncoderLayer(nn.Module):
    def __init__(self,
                 d_model,
                 nhead,
                 dim_feedforward=2048,
                 dropout=0.1,
                 attention_dropout=0.1,
                 activation_dropout=0.1,
                 activation='relu',
                 normalize_before=True):
        super(TransformerEncoderLayer, self).__init__()
        self.normalize_before = normalize_before
        self.self_attn = nn.MultiheadAttention(d_model,
                                               nhead,
                                               dropout=attention_dropout)
        # Implementation of Feedforward model
        self.linear1 = Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.activation_dropout = nn.Dropout(activation_dropout)

        self.activation = {'relu': F.relu, 'gelu': F.gelu}[activation]

    def forward(self, src, src_mask=None, src_key_padding_mask=None):
        r"""Pass the input through the endocder layer.

        Args:
            src: the sequnce to the encoder layer (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).
        """
        # Self attention block
        residual = src
        if self.normalize_before:
            src = self.norm1(src)
        src = self.self_attn(src,
                             src,
                             src,
                             attn_mask=src_mask,
                             key_padding_mask=src_key_padding_mask)[0]
        src = residual + self.dropout(src)
        if not self.normalize_before:
            src = self.norm1(src)
        # Feed forward block
        residual = src
        if self.normalize_before:
            src = self.norm2(src)
        src = self.activation(self.linear1(src))
        src = self.activation_dropout(src)
        src = self.linear2(src)
        src = residual + self.dropout(src)
        if not self.normalize_before:
            src = self.norm2(src)
        return src


class Transformer(nn.Module):
    def __init__(self, *, src_vocab_size, trg_vocab_size, embed_dim, nb_heads,
                 src_hid_size, src_nb_layers, trg_hid_size, trg_nb_layers,
                 dropout_p, tie_trg_embed, src_c2i, trg_c2i, attr_c2i,
                 label_smooth, **kwargs):
        '''
        init
        '''
        super().__init__()
        self.src_vocab_size = src_vocab_size
        self.trg_vocab_size = trg_vocab_size
        self.embed_dim = embed_dim
        self.embed_scale = math.sqrt(embed_dim)
        self.nb_heads = nb_heads
        self.src_hid_size = src_hid_size
        self.src_nb_layers = src_nb_layers
        self.trg_hid_size = trg_hid_size
        self.trg_nb_layers = trg_nb_layers
        self.dropout_p = dropout_p
        self.tie_trg_embed = tie_trg_embed
        self.label_smooth = label_smooth
        self.src_c2i, self.trg_c2i, self.attr_c2i = src_c2i, trg_c2i, attr_c2i
        self.src_embed = Embedding(src_vocab_size,
                                   embed_dim,
                                   padding_idx=PAD_IDX)
        self.trg_embed = Embedding(trg_vocab_size,
                                   embed_dim,
                                   padding_idx=PAD_IDX)
        self.position_embed = SinusoidalPositionalEmbedding(embed_dim, PAD_IDX)
        encoder_layer = TransformerEncoderLayer(d_model=embed_dim,
                                                nhead=nb_heads,
                                                dim_feedforward=src_hid_size,
                                                dropout=dropout_p,
                                                attention_dropout=dropout_p,
                                                activation_dropout=dropout_p,
                                                normalize_before=True)
        self.encoder = nn.TransformerEncoder(encoder_layer,
                                             num_layers=src_nb_layers,
                                             norm=nn.LayerNorm(embed_dim))
        decoder_layer = TransformerDecoderLayer(d_model=embed_dim,
                                                nhead=nb_heads,
                                                dim_feedforward=trg_hid_size,
                                                dropout=dropout_p,
                                                attention_dropout=dropout_p,
                                                activation_dropout=dropout_p,
                                                normalize_before=True)
        self.decoder = nn.TransformerDecoder(decoder_layer,
                                             num_layers=trg_nb_layers,
                                             norm=nn.LayerNorm(embed_dim))
        self.final_out = Linear(embed_dim, trg_vocab_size)
        if tie_trg_embed:
            self.final_out.weight = self.trg_embed.weight
        self.dropout = nn.Dropout(dropout_p)
        # self._reset_parameters()

    def embed(self, src_batch, src_mask):
        word_embed = self.embed_scale * self.src_embed(src_batch)
        pos_embed = self.position_embed(src_batch)
        embed = self.dropout(word_embed + pos_embed)
        return embed

    def encode(self, src_batch, src_mask):
        embed = self.embed(src_batch, src_mask)
        return self.encoder(embed, src_key_padding_mask=src_mask)

    def decode(self, enc_hs, src_mask, trg_batch, trg_mask):
        word_embed = self.embed_scale * self.trg_embed(trg_batch)
        pos_embed = self.position_embed(trg_batch)
        embed = self.dropout(word_embed + pos_embed)

        trg_seq_len = trg_batch.size(0)
        causal_mask = self.generate_square_subsequent_mask(trg_seq_len)
        dec_hs = self.decoder(embed,
                              enc_hs,
                              tgt_mask=causal_mask,
                              tgt_key_padding_mask=trg_mask,
                              memory_key_padding_mask=src_mask)
        return F.log_softmax(self.final_out(dec_hs), dim=-1)

    def forward(self, src_batch, src_mask, trg_batch, trg_mask):
        '''
        only for training
        '''
        src_mask = (src_mask == 0).transpose(0, 1)
        trg_mask = (trg_mask == 0).transpose(0, 1)
        enc_hs = self.encode(src_batch, src_mask)
        # output: [trg_seq_len, batch_size, vocab_size]
        output = self.decode(enc_hs, src_mask, trg_batch, trg_mask)
        return output

    def count_nb_params(self):
        model_parameters = filter(lambda p: p.requires_grad, self.parameters())
        params = sum([np.prod(p.size()) for p in model_parameters])
        return params

    def loss(self, predict, target):
        '''
        compute loss
        '''
        predict = predict.view(-1, self.trg_vocab_size)
        # nll_loss = F.nll_loss(predict, target.view(-1), ignore_index=PAD_IDX)
        target = target.view(-1, 1)
        non_pad_mask = target.ne(PAD_IDX)
        nll_loss = -predict.gather(dim=-1, index=target)[non_pad_mask].mean()
        smooth_loss = -predict.sum(dim=-1, keepdim=True)[non_pad_mask].mean()
        smooth_loss = smooth_loss / self.trg_vocab_size
        loss = (1. -
                self.label_smooth) * nll_loss + self.label_smooth * smooth_loss
        return loss

    def get_loss(self, data):
        src, src_mask, trg, trg_mask = data
        out = self.forward(src, src_mask, trg, trg_mask)
        loss = self.loss(out[:-1], trg[1:])
        return loss

    def get_correct_preds(self, data):
        src, src_mask, trg, trg_mask = data
        out = self.forward(src, src_mask, trg, trg_mask)
        preds = out[:-1].topk(1)[1].squeeze()
        trg_y = trg[1:]
        non_pad_mask = trg_y.ne(PAD_IDX).int()
        preds = preds * non_pad_mask
        correct_preds = torch.all(torch.eq(preds, trg_y), dim=0).sum()
        return correct_preds

    def generate_square_subsequent_mask(self, sz):
        r"""Generate a square mask for the sequence. The masked positions are filled with float('-inf').
            Unmasked positions are filled with float(0.0).
        """
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(
            mask == 1, float(0.0))
        return mask.to(DEVICE)


class TagTransformer(Transformer):
    def __init__(self, *, nb_attr, **kwargs):
        super().__init__(**kwargs)
        self.nb_attr = nb_attr
        # 0 -> special token & tags, 1 -> character
        self.special_embeddings = Embedding(2, self.embed_dim)

    def embed(self, src_batch, src_mask):
        word_embed = self.embed_scale * self.src_embed(src_batch)
        char_mask = (src_batch < (self.src_vocab_size - self.nb_attr)).long()
        special_embed = self.embed_scale * self.special_embeddings(char_mask)
        pos_embed = self.position_embed(src_batch * char_mask)
        embed = self.dropout(word_embed + pos_embed + special_embed)
        return embed


class UniversalTransformerEncoder(nn.Module):
    def __init__(self, encoder_layer, num_layers, norm=None):
        super(UniversalTransformerEncoder, self).__init__()
        self.encoder_layer = encoder_layer
        self.num_layers = num_layers
        self.norm = norm

    def forward(self, src, mask=None, src_key_padding_mask=None):
        output = src

        for i in range(self.num_layers):
            output = self.encoder_layer(
                output,
                src_mask=mask,
                src_key_padding_mask=src_key_padding_mask)

        if self.norm:
            output = self.norm(output)

        return output


class UniversalTransformerDecoder(nn.Module):
    def __init__(self, decoder_layer, num_layers, norm=None):
        super(UniversalTransformerDecoder, self).__init__()
        self.decoder_layer = decoder_layer
        self.num_layers = num_layers
        self.norm = norm

    def forward(self,
                tgt,
                memory,
                tgt_mask=None,
                memory_mask=None,
                tgt_key_padding_mask=None,
                memory_key_padding_mask=None):
        output = tgt

        for i in range(self.num_layers):
            output = self.decoder_layer(
                output,
                memory,
                tgt_mask=tgt_mask,
                memory_mask=memory_mask,
                tgt_key_padding_mask=tgt_key_padding_mask,
                memory_key_padding_mask=memory_key_padding_mask)

        if self.norm:
            output = self.norm(output)

        return output


class UniversalTransformer(Transformer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        encoder_layer = TransformerEncoderLayer(
            d_model=self.embed_dim,
            nhead=self.nb_heads,
            dim_feedforward=self.src_hid_size,
            dropout=self.dropout_p,
            attention_dropout=self.dropout_p,
            activation_dropout=self.dropout_p,
            normalize_before=True)
        self.encoder = UniversalTransformerEncoder(
            encoder_layer,
            num_layers=self.src_nb_layers,
            norm=nn.LayerNorm(self.embed_dim))
        decoder_layer = TransformerDecoderLayer(
            d_model=self.embed_dim,
            nhead=self.nb_heads,
            dim_feedforward=self.trg_hid_size,
            dropout=self.dropout_p,
            attention_dropout=self.dropout_p,
            activation_dropout=self.dropout_p,
            normalize_before=True)
        self.decoder = UniversalTransformerDecoder(
            decoder_layer,
            num_layers=self.trg_nb_layers,
            norm=nn.LayerNorm(self.embed_dim))


class TagUniversalTransformer(TagTransformer, UniversalTransformer):
    pass


def Embedding(num_embeddings, embedding_dim, padding_idx=None):
    m = nn.Embedding(num_embeddings, embedding_dim, padding_idx=padding_idx)
    nn.init.normal_(m.weight, mean=0, std=embedding_dim**-0.5)
    if padding_idx is not None:
        nn.init.constant_(m.weight[padding_idx], 0)
    return m


def Linear(in_features, out_features, bias=True):
    m = nn.Linear(in_features, out_features, bias)
    nn.init.xavier_uniform_(m.weight)
    if bias:
        nn.init.constant_(m.bias, 0.)
    return m


class PointerGeneratorTransformer(Transformer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Pointer generator decoder outputs
        decoder_layer = TransformerDecoderLayer(d_model=self.embed_dim,
                                                nhead=self.nb_heads,
                                                dim_feedforward=self.trg_hid_size,
                                                dropout=self.dropout_p,
                                                attention_dropout=self.dropout_p,
                                                activation_dropout=self.dropout_p,
                                                normalize_before=True)
        decoder_final_layer = TransformerDecoderFinalLayer(d_model=self.embed_dim,
                                                           nhead=self.nb_heads,
                                                           dim_feedforward=self.trg_hid_size,
                                                           dropout=self.dropout_p,
                                                           attention_dropout=self.dropout_p,
                                                           activation_dropout=self.dropout_p,
                                                           normalize_before=True)
        self.decoder = TransformerDecoder(decoder_layer,
                                          decoder_final_layer,
                                          num_layers=self.trg_nb_layers,
                                          norm=nn.LayerNorm(self.embed_dim))
        # -----------Pointer-Generator Additions------------
        self.input_to_output_vocab_conversion_matrix = self.get_conversion_matrix()
        # Final linear layer + softmax. for probability over target vocabulary
        self.p_vocab = nn.Sequential(
            nn.Linear(self.embed_dim, self.trg_vocab_size),
            nn.Softmax(dim=-1))

        # P_gen, probability of generating output
        self.p_gen = nn.Sequential(
            nn.Linear(self.embed_dim * 3, 1),
            nn.Sigmoid())
        # Context vector
        self.c_t = None
        # self._reset_parameters()

    def get_conversion_matrix(self):
        # Initialize conversion matrix
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        src_to_tgt_conversion_matrix = torch.zeros(self.src_vocab_size, self.trg_vocab_size, device=device)
        input_vocab = {**self.src_c2i, **self.attr_c2i}
        input_vocab_items = input_vocab.items()
        output_vocab = self.trg_c2i
        # Go over all (token, id) items in input vocab
        for src_token, src_id in input_vocab_items:
            tgt_id = output_vocab.get(src_token, UNK_IDX)
            src_to_tgt_conversion_matrix[src_id][tgt_id] = 1
        return src_to_tgt_conversion_matrix


    def decode(self, enc_hs, src_mask, trg_batch, trg_mask, src):
        word_embed = self.embed_scale * self.trg_embed(trg_batch)
        pos_embed = self.position_embed(trg_batch)
        embed = self.dropout(word_embed + pos_embed)

        trg_seq_len = trg_batch.size(0)
        causal_mask = self.generate_square_subsequent_mask(trg_seq_len)
        dec_hs, attention = self.decoder(embed,
                                         enc_hs,
                                         tgt_mask=causal_mask,
                                         tgt_key_padding_mask=trg_mask,
                                         memory_key_padding_mask=src_mask)
        # ---Compute Pointer Generator probability---
        # Get probability over target vocabulary, (T, N, E) -> (T, N, tgt_vocab_size)
        p_vocab = self.p_vocab(dec_hs)
        # Get hidden states of source (easier/more understandable computation). (S, N, E) -> (N, S, E)
        hidden_states = enc_hs.transpose(0, 1)
        # compute context vectors. (N, T, S) x (N, S, E) -> (N, T, E)
        context_vectors = torch.matmul(attention, hidden_states).transpose(0, 1)
        total_states = torch.cat((context_vectors, dec_hs, embed), dim=-1)
        # Get probability of generating output. (N, T, 3*E) -> (N, T, 1)
        p_gen = self.p_gen(total_states)
        # Get probability of copying from input. (N, T, 1)
        p_copy = 1 - p_gen

        # Get representation of src tokens as one hot encoding
        one_hot = torch.zeros(src.size(0), src.size(1), self.src_vocab_size, device=src.device)
        one_hot = one_hot.scatter_(dim=-1, index=src.unsqueeze(-1), value=1)
        # p_copy from source is sum over all attention weights for each token in source
        p_copy_src_vocab = torch.matmul(attention, one_hot)
        # convert representation of token from src vocab to tgt vocab
        p_copy_tgt_vocab = torch.matmul(p_copy_src_vocab, self.input_to_output_vocab_conversion_matrix).transpose(0, 1)
        # Compute final probability
        p = torch.add(p_vocab * p_gen, p_copy_tgt_vocab * p_copy)
        # Change back batch and sequence dimensions, from (T, N, tgt_vocab_size) -> (N, T, tgt_vocab_size)
        return torch.log(p)

    def forward(self, src_batch, src_mask, trg_batch, trg_mask):
        '''
        only for training
        '''
        # -----------
        # print(src_batch.size(), src_mask.size(), trg_batch.size(), trg_mask.size())
        # print(src_batch)
        # print(src_mask)
        # print(trg_batch)
        # -----------
        src_mask = (src_mask == 0).transpose(0, 1)
        trg_mask = (trg_mask == 0).transpose(0, 1)
        enc_hs = self.encode(src_batch, src_mask)
        # output: [trg_seq_len, batch_size, vocab_size]
        output = self.decode(enc_hs, src_mask, trg_batch, trg_mask, src_batch.transpose(0, 1))
        return output

