# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
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
# ==============================================================================

"""This file contains code to run beam search decoding"""

import tensorflow as tf
import numpy as np
import data

FLAGS = tf.app.flags.FLAGS

class Hypothesis(object):
  """Class to represent a hypothesis during beam search. Holds all the information needed for the hypothesis."""

  def __init__(self, tokens, log_probs, state, attn_dists, p_gens, coverage):
    """Hypothesis constructor.

    Args:
      tokens: List of integers. The ids of the tokens that form the summary so far.
      log_probs: List, same length as tokens, of floats, giving the log probabilities of the tokens so far.
      state: Current state of the decoder, a LSTMStateTuple.
      attn_dists: List, same length as tokens, of numpy arrays with shape (attn_length). These are the attention distributions so far.
      p_gens: List, same length as tokens, of floats, or None if not using pointer-generator model. The values of the generation probability so far.
      coverage: Numpy array of shape (attn_length), or None if not using coverage. The current coverage vector.
    """
    self.tokens = tokens
    self.log_probs = log_probs
    self.state = state
    self.attn_dists = attn_dists
    self.p_gens = p_gens
    self.coverage = coverage

  def extend(self, token, log_prob, state, attn_dist, p_gen, coverage):
    """Return a NEW hypothesis, extended with the information from the latest step of beam search.

    Args:
      token: Integer. Latest token produced by beam search.
      log_prob: Float. Log prob of the latest token.
      state: Current decoder state, a LSTMStateTuple.
      attn_dist: Attention distribution from latest step. Numpy array shape (attn_length).
      p_gen: Generation probability on latest step. Float.
      coverage: Latest coverage vector. Numpy array shape (attn_length), or None if not using coverage.
    Returns:
      New Hypothesis for next step.
    """
    return Hypothesis(tokens = self.tokens + [token],
                      log_probs = self.log_probs + [log_prob],
                      state = state,
                      attn_dists = self.attn_dists + [attn_dist],
                      p_gens = self.p_gens + [p_gen],
                      coverage = coverage)

  @property
  def latest_token(self):
    return self.tokens[-1]

  def _has_unknown_token(self, stop_token_id):
    if any(token < data.N_FREE_TOKENS for token in self.tokens[1:-1]):
      return True
    if self.latest_token < data.N_FREE_TOKENS and self.latest_token != stop_token_id:
      return True

    return False

  def avg_log_prob(self, stop_token_id):
    if self._has_unknown_token(stop_token_id):
      return -10 ** 6
    return sum(self.log_probs) / len(self.tokens)

    """
    # Compute average log_prob per step. Weigh the generative and copy parts equally so that we
    # don't bias towards sequences of only copying (which have higher log_probs generally).
    gen_sum = sum(self.p_gens)
    gen_score = sum(
      p_gen / gen_sum * log_prob for p_gen, log_prob in zip(self.p_gens, self.log_probs)
    )
    copy_sum = sum(1. - p_gen for p_gen in self.p_gens)
    copy_score = sum(
      (1. - p_gen) / copy_sum * log_prob for p_gen, log_prob in zip(self.p_gens, self.log_probs)
    )
    return .5 * gen_score + .5 * copy_score
    """

  def score(self, stop_token_id, start_sent_ids, stopword_ids, pronoun_ids):
    if self._has_unknown_token(stop_token_id):
      return -10 ** 6

    avg_log_prob = self._smart_avg_log_prob(start_sent_ids, stopword_ids, pronoun_ids)
    return avg_log_prob - self.repeated_n_gram_loss - self.cov_loss

  def _smart_avg_log_prob(self, start_sent_ids, stopword_ids, pronoun_ids):
    sentence_start_weights = np.zeros((len(self.tokens),), dtype=np.float32)
    log_probs = np.array(self.log_probs)

    for i, token in enumerate(self.tokens):
      if token in start_sent_ids:
        for j in range(i + 1, min(len(self.tokens), i + 5)):
          if self.tokens[j] not in stopword_ids:
            sentence_start_weights[j] = 1. / (j - i + 5)

      if token in pronoun_ids:
        log_probs[i] -= .8

    sentence_start_weights /= sentence_start_weights.sum()
    sentence_start_log_probs = sentence_start_weights.dot(log_probs)

    return .75 * log_probs.mean() + .25 * sentence_start_log_probs

  @property
  def avg_top_attn(self):
      return sum(max(attn_dist) for attn_dist in self.attn_dists) / len(self.attn_dists)

  @property
  def cov_loss(self):
    coverage = np.zeros_like(self.attn_dists[0]) # shape (batch_size, attn_length).
    covlosses = []  # Coverage loss per decoder timestep. Will be list length max_dec_steps containing shape (batch_size).

    for a in self.attn_dists:
      covloss = np.minimum(a, coverage).sum()  # calculate the coverage loss for this step
      covlosses.append(covloss)
      coverage += a  # update the coverage vector

    return sum(covlosses) / len(covlosses)

  @property
  def repeated_n_gram_loss(self, disallowed_n=3):
    seen_n_grams = set()

    for i in range(len(self.tokens) - disallowed_n + 1):
      n_gram = tuple(self.tokens[i: i + disallowed_n])
      if n_gram in seen_n_grams:
        return 10. ** 6
      seen_n_grams.add(n_gram)

    return 0.


def run_beam_search(sess, model, vocab, batch):
  """Performs beam search decoding on the given example.

  Args:
    sess: a tf.Session
    model: a seq2seq model
    vocab: Vocabulary object
    batch: Batch object that is the same example repeated across the batch

  Returns:
    best_hyp: Hypothesis object; the best hypothesis found by beam search.
  """
  # Run the encoder to get the encoder hidden states and decoder initial state
  enc_states, dec_in_state = model.run_encoder(sess, batch)
  # dec_in_state is a LSTMStateTuple
  # enc_states has shape [batch_size, <=max_enc_steps, 2*enc_hidden_dim].

  # Initialize beam_size-many hyptheses
  hyps = [Hypothesis(tokens=[vocab.word2id(data.START_DECODING, None)],
                     log_probs=[0.0],
                     state=dec_in_state,
                     attn_dists=[],
                     p_gens=[],
                     coverage=np.zeros([batch.enc_batch.shape[1]]) # zero vector of length attention_length
                     ) for _ in xrange(FLAGS.beam_size)]
  results = [] # this will contain finished hypotheses (those that have emitted the [STOP] token)

  stop_token_id = vocab.word2id(data.STOP_DECODING, None)
  start_sent_ids = {vocab.word2id(word, None) for word in (data.START_DECODING, '.')}
  stopword_ids = {vocab.word2id(word, None) for word in (
    'the', 'a', 'an', 'it', 'its', 'this', 'that', 'these', 'those'
  )}
  pronoun_ids = {vocab.word2id(word, None) for word in (
    'he', 'she', 'him', 'her', 'i', 'we'
  )}

  steps = 0
  while steps < FLAGS.max_dec_steps and len(results) < 4 * FLAGS.beam_size:
    latest_tokens = [h.latest_token for h in hyps] # latest token produced by each hypothesis
    # change any in-article temporary OOV ids to [UNK] id, so that we can lookup word embeddings
    latest_tokens = [batch.article_id_to_word_ids[0].get(t, t) for t in latest_tokens]
    states = [h.state for h in hyps] # list of current decoder states of the hypotheses
    prev_coverage = [h.coverage for h in hyps] # list of coverage vectors (or None)

    # Run one step of the decoder to get the new info
    topk_ids, topk_log_probs, new_states, attn_dists, p_gens, new_coverage = model.decode_onestep(
      sess=sess,
      batch=batch,
      latest_tokens=latest_tokens,
      enc_states=enc_states,
      dec_init_states=states,
      prev_coverage=prev_coverage,
    )

    # Extend each hypothesis and collect them all in all_hyps
    all_hyps = []
    num_orig_hyps = 1 if steps == 0 else len(hyps) # On the first step, we only had one original hypothesis (the initial hypothesis). On subsequent steps, all original hypotheses are distinct.
    for i in xrange(num_orig_hyps):
      h, new_state, attn_dist, p_gen, new_coverage_i = hyps[i], new_states[i], attn_dists[i], p_gens[i], new_coverage[i]  # take the ith hypothesis and new decoder state info
      for j in xrange(FLAGS.beam_size * 2):  # for each of the top 2*beam_size hyps:
        # Extend the ith hypothesis with the jth option
        new_hyp = h.extend(token=topk_ids[i, j],
                           log_prob=topk_log_probs[i, j],
                           state=new_state,
                           attn_dist=attn_dist,
                           p_gen=p_gen,
                           coverage=new_coverage_i)
        all_hyps.append(new_hyp)

    # Filter and collect any hypotheses that have produced the end token.
    hyps = [] # will contain hypotheses for the next step
    for h in sort_hyps(all_hyps, stop_token_id, start_sent_ids, stopword_ids, pronoun_ids): # in order of most likely h
      if h.latest_token == vocab.word2id(data.STOP_DECODING, None): # if stop token is reached...
        # If this hypothesis is sufficiently long, put in results. Otherwise discard.
        if steps >= FLAGS.min_dec_steps:
          results.append(h)
      elif h.latest_token >= data.N_FREE_TOKENS:
        # hasn't reached stop token and generated non-unk token, so continue to extend this hypothesis
        hyps.append(h)
      if len(hyps) == FLAGS.beam_size or len(results) == 4 * FLAGS.beam_size:
        # Once we've collected beam_size-many hypotheses for the next step, or beam_size-many complete hypotheses, stop.
        break

    steps += 1

  # At this point, either we've got 4 * beam_size results, or we've reached maximum decoder steps

  if len(results)==0: # if we don't have any complete results, add all current hypotheses (incomplete summaries) to results
    results = hyps

  # Sort hypotheses by average log probability
  hyps_sorted = sort_hyps(results, stop_token_id, start_sent_ids, stopword_ids, pronoun_ids)

  # Return the hypothesis with highest average log prob
  return hyps_sorted[0]

def sort_hyps(hyps, stop_token_id, start_sent_ids, stopword_ids, pronoun_ids):
  """Return a list of Hypothesis objects, sorted by descending average log probability"""
  if FLAGS.smart_decode:
    score_func = lambda h: h.score(stop_token_id, start_sent_ids, stopword_ids, pronoun_ids)
  else:
    score_func = lambda h: h.avg_log_prob(stop_token_id)

  return sorted(hyps, key=score_func, reverse=True)
