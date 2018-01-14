#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2017 Yifan WANG <yifanwang1993@gmail.com>
#
# Distributed under terms of the MIT license.

"""
Utility functions
"""
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
from torch.utils import data
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from torch.distributions import Categorical
import os, time, sys, datetime, argparse, pickle
import spacy
from torchtext.vocab import Vocab
# from torchtext.vocab import GloVe
from torchtext.data import Field, Pipeline, RawField, Dataset, Example, BucketIterator
from torchtext.data import get_tokenizer

# TODO: add these into configuration
EOS = "<eos>"
SOS = "<sos>"
PAD = "<pad>"

def split_data(root, filenames, exts, train_ratio=0.8, test_ratio=0.2):
    """
    Examples: filenames = ['en.txt', 'fr.txt'], exts = ['src', 'trg']
              => train.src, train.trg; test.src, test.trg
    """
    # TODO: check the extension names
    eps = 1e-5
    valid_ratio = 1 - train_ratio - test_ratio
    p = None
    for name, ext in zip(filenames, exts):
        print("Opening {0}".format(name))
        with open(root + name, 'r') as f:
            lines = f.readlines()
            n = len(lines)
            p = np.random.permutation(n) if p is None else p
            train, test, valid = np.split(np.arange(n)[p], [int(n*train_ratio), int(n*train_ratio+n*test_ratio)])

            train = [lines[i] for i in train]
            test = [lines[i] for i in test]
            valid = [lines[i] for i in valid] if valid_ratio > eps else valid
            for samples, mode in [(train, 'train'), (test, 'test'), (valid, 'valid')]:
                if valid_ratio < eps and mode == 'valid':
                    continue
                out = open(root + mode + ext, 'w')
                for l in samples:
                    out.write(l.strip() + '\n')
                out.close()
            print("Train: {0}\nTest: {1}\nValidation: {2}".format(len(train), len(test), len(valid)))


def stoi(s, field):
    sent = [field.vocab.stoi[w] for w in s]
    return sent

def itos(s, field):
    sent = " ".join([field.vocab.itos[w] for w in s])
    return sent.strip()

def since(t):
    return '[' + str(datetime.timedelta(seconds=time.time() - t)) + '] '


def load_data(c):
    """
    Load datasets, return a dictionary of datasets and fields
    """

    # TODO: add field for context

    spacy_src = spacy.load(c['src_lang'])
    spacy_trg = spacy.load(c['trg_lang'])

    def tokenize_src(text):
        return [tok.text for tok in spacy_src.tokenizer(text)]

    def tokenize_trg(text):
        return [tok.text for tok in spacy_trg.tokenizer(text)]

    src_field = Field(tokenize=tokenize_src, include_lengths=True, eos_token=EOS, lower=True)
    trg_field= Field(tokenize=tokenize_trg, include_lengths=True, eos_token=EOS, lower=True, init_token=SOS)

    datasets = {}
    # load processed data
    for split in c['splits']:
        if os.path.isfile(c['root'] + split + '.pkl'):
            print('Loading {0}'.format(c['root'] + split + '.pkl'))
            examples = pickle.load(open(c['root'] + split + '.pkl', 'rb'))
            datasets[split] = Dataset(examples = examples, fields={'src':src_field,'trg': trg_field})
        else:
            src_path = c['root'] + split + '.src'
            trg_path = c['root'] + split + '.trg'
            examples = c['load'](src_path, trg_path, src_field, trg_field)
            datasets[split] = Dataset(examples = examples, fields={'src':src_field,'trg': trg_field})
            print('Saving to {0}'.format(c['root'] + split + '.pkl'))
            pickle.dump(examples, open(c['root'] + split + '.pkl', 'wb'))

    return datasets, src_field, trg_field


def evaluate(encoder, decoder, var, trg_field, max_len=30, beam_size=-1):
    """
    var: tuple of tensors
    """
    logsm = nn.LogSoftmax()
    # Beam search
    # TODO: check the beam search
    H = [([SOS], 0.)]
    H_temp = []
    H_final = []

    outputs = []
    encoder_inputs, encoder_lengths = var
    encoder_packed, encoder_hidden = encoder(encoder_inputs, encoder_lengths)
    encoder_unpacked = pad_packed_sequence(encoder_packed)[0]
    decoder_hidden = encoder_hidden
    decoder_inputs, decoder_lenghts = trg_field.numericalize(([[SOS]], [1]), device=-1)

    if beam_size > 0:
        for i in range(max_len):
            for h in H:
                hyp, s = h
                decoder_inputs, decoder_lenghts = trg_field.numericalize(([hyp], [len(hyp)]), device=-1)
                decoder_unpacked, decoder_hidden = decoder(decoder_inputs, decoder_hidden, encoder_unpacked, encoder_lengths)
                topv, topi = decoder_unpacked.data[-1].topk(beam_size)
                topv = logsm(topv)
                for j in range(beam_size):
                    nj = int(topi.numpy()[0][j])
                    hyp_new = hyp + [trg_field.vocab.itos[nj]]
                    s_new = s + topv.data.numpy().tolist()[-1][j]
                    if trg_field.vocab.itos[nj] == EOS:
                        H_final.append((hyp_new, s_new))
                    else:
                        H_temp.append((hyp_new, s_new))
                H_temp = sorted(H_temp, key=lambda x:x[1], reverse=True)
                H = H_temp[:beam_size]
                H_temp = []

        H_final = sorted(H_final, key=lambda x:x[1], reverse=True)
        outputs = [" ".join(H_final[i][0]) for i in range(beam_size)]

    else:
        for i in range(max_len):
            # Eval mode, dropout is not used
            decoder_unpacked, decoder_hidden = decoder.eval()(decoder_inputs, decoder_hidden, encoder_unpacked, encoder_lengths)
            topv, topi = decoder_unpacked.data.topk(1)
            ni = int(topi.numpy()[0][0][0])
            if trg_field.vocab.itos[ni] == EOS:
                outputs.append(EOS)
                break
            else:
                outputs.append(trg_field.vocab.itos[ni])
            decoder_inputs = Variable(torch.LongTensor([[ni]]))
        outputs = " ".join(outputs)
    return outputs.strip()

def sample(encoder, decoder, var, trg_field, max_len=30, greedy=True):
    """ Sample an output given the input
    """
    sm = nn.Softmax()

    outputs = []
    encoder_inputs, encoder_lengths = var
    encoder_packed, encoder_hidden = encoder(encoder_inputs, encoder_lengths)
    encoder_unpacked = pad_packed_sequence(encoder_packed)[0]
    decoder_hidden = encoder_hidden
    decoder_inputs, decoder_lenghts = trg_field.numericalize(([[SOS]], [1]), device=-1)
    for i in range(max_len):
        # TODO: shall we use eval mode?
        # decoder_unpacked: (1, 1, vocab_size)
        decoder_unpacked, decoder_hidden = decoder.eval()(decoder_inputs, decoder_hidden, encoder_unpacked, encoder_lengths)
        if greedy:
            topv, topi = decoder_unpacked.data.topk(1)
            tv, ti = sm(decoder_unpacked.squeeze()).data.topk(10)
            print(tv)
            # ni must be an integer, not like numpy.int32
            ni = int(topi.numpy()[0][0][0])
        else:
            m = Categorical(sm(decoder_unpacked.squeeze()))
            ni = m.sample()
            ni = int(ni.data.numpy()[0])

        if trg_field.vocab.itos[ni] == EOS:
            outputs.append(EOS)
            break
        else:
            outputs.append(trg_field.vocab.itos[ni])
        decoder_inputs = Variable(torch.LongTensor([[ni]]))
    outputs = " ".join(outputs)
    return outputs.strip()


def random_eval(encoder, decoder, batch, n, src_field, trg_field, beam_size=-1):
    print("Random sampling...")
    enc_inputs, enc_lengths = batch.src
    dec_inputs, dec_lengths = batch.trg
    N = enc_inputs.size()[1]
    idx = np.random.choice(N, n)
    for i in idx:
        print('\t> ' + itos(enc_inputs[:,i].data.numpy(), src_field))
        print('\t= ' + itos(dec_inputs[:,i].data.numpy(), trg_field))
        eval_input = (enc_inputs[:,i].unsqueeze(1), torch.LongTensor([enc_lengths[i]]))
        sent = evaluate(encoder, decoder, eval_input, trg_field=trg_field, beam_size=beam_size)
        print('\t< ' + sent)
        print()

def cuda(var, use_cuda):
    if use_cuda:
        var = var.cuda()
    return var
