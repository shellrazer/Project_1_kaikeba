import tensorflow as tf
from pgn import PGN
from data_loader import batch, get_token, token_to_word
import numpy as np
from gensim.models import Word2Vec
import os
import time
from rouge import Rouge


def loss_function(real, pred, padding_mask):
    #  pred is list of list, max_len_y * [batch_sz, extend_vocabsize] (without argmax) real & mask [batch_sz, max_len_y]
    #mask = tf.math.logical_not(tf.math.equal(real, 0))
    loss = 0
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(reduction="none")
    for t in range(real.shape[1]-1):
        loss_ = loss_object(real[:,t+1], pred[t])    # note this change 11.11 [batch_sz,]
        mask = tf.cast(padding_mask[:,t+1], dtype=loss_.dtype)
        loss_ *= mask
        # print('loss_:', loss_)
        loss_ = tf.reduce_mean(loss_, axis=0)  # batch-wise
        loss += loss_
        # print('loss and loss at each time step(batch sum)', loss, loss_)
    tf.print("loss:",loss)
    return loss


def coverage_loss(attn_dists, coverages, padding_mask):
    # attn_dists: [max_len_y, batch_sz, max_len_x, 1]
    attn_dists = tf.squeeze(attn_dists, axis=-1)   # [max_len_y, batch_sz, max_len]
    coverage = tf.zeros_like(attn_dists[0])  # shape (batch_size, max_len_x). Initial coverage is zero.
    covlosses = []  # Coverage loss per decoder timestep. Will be list length max_dec_steps containing shape (batch_size).
    padding_mask = tf.stack(padding_mask, 1)   # [max_len_y, batch_sz]
    mask = tf.cast(padding_mask, dtype=attn_dists[1].dtype)
    for i,a in enumerate(attn_dists):
        covloss = tf.reduce_sum(tf.minimum(a, coverage), [1])  # calculate the coverage loss for this step
        # update the coverage vector
        coverage += a
        covloss = covloss * mask[i,:]
        covlosses.append(covloss)
    loss = tf.reduce_sum(tf.reduce_mean(covlosses, axis=0))
    tf.print('coverage loss(batch sum):', loss)
    return loss


#@tf.function
def train_one_batch(mode, w2v_model, model, optimizer, oov_dict, st, inp, targ, enc_extended_inp, enc_pad_mask,
                    batch_oov_len, cov_loss_wt, padding_mask=None):
    with tf.GradientTape() as tape:
        # call(self, enc_inp, dec_inp, enc_extended_inp, batch_oov_len, use_coverage=True, prev_coverage=None)
        # final_dist [batch_sz, max_len_y, extend_vocab_size] attentions, coverages [max_len_y, batch_sz, max_len_x, 1]
        # 1110 change final_dist a list of max_len_y length of [batch_sz, extend_vocab_size]
        enc_output, enc_hidden = model.call_encoder(inp)
        final_dist, attentions, coverages, _, _, _ = model(targ, enc_extended_inp, enc_pad_mask, batch_oov_len,
                                                           enc_output, enc_hidden, use_coverage=True,prev_coverage=None)
        loss = loss_function(targ,final_dist,padding_mask)+cov_loss_wt*coverage_loss(attentions,coverages,padding_mask)
        batch_loss = (loss / int(targ.shape[1]))
        if mode == 'train':
            variables = model.encoder.trainable_variables + model.attention.trainable_variables + \
                        model.decoder.trainable_variables + model.pointer.trainable_variables
            # print(variables) count total variables
            # print("total parameters:", np.sum([np.prod(v.get_shape().as_list()) for v in variables]))
            # print("parameters are:", [v.name for v in variables])
            gradients = tape.gradient(loss, variables)
            optimizer.apply_gradients(zip(gradients, variables))
            return batch_loss

        if mode == 'test':
            print('********result of test at batch', st/64, '*********')
            final_dist = tf.convert_to_tensor(final_dist)
            final_dist = tf.argmax(final_dist, axis=-1)
            final_dist = tf.stack(final_dist, 1)   # change to [batch_sz, max_len_y]
            preds = []
            reals = []
            for i in range(final_dist.shape[0]):
                reals.append(token_to_word(w2v_model, targ[i], oov_dict[st+i]))
                preds.append('<START>' + token_to_word(w2v_model, final_dist[i], oov_dict[st+i]))
                if i % 8 == 0:
                    print(reals[i])
                    print(preds[i])
            return batch_loss, preds, reals


def train(mode, w2v_model, model, optimizer, checkpoint, checkpoint_prefix, save_chkp_epoch,
          dataset_batch, dataset_len, dataset_oov_dict, batch_sz, EPOCHS, cov_loss_wt):

    print('dataset_len:', dataset_len)
    steps_per_epoch = dataset_len // batch_sz
    for epoch in range(EPOCHS):
        start = time.time()
        total_loss = 0
        for (batch, (enc, enc_extend, enc_mask, enc_oov_len, dec, dec_mask)) in enumerate(dataset_batch.take(steps_per_epoch)):
            # record the sample number
            st = (batch + 1) * batch_sz
            # (mode, oov_dict, st, inp, targ, enc_extended_inp, enc_pad_mask, batch_oov_len, cov_loss_wt, padding_mask=None)
            batch_loss = train_one_batch(mode,w2v_model,model,optimizer,dataset_oov_dict,st,enc,dec,enc_extend,enc_mask,
                                         enc_oov_len, cov_loss_wt=cov_loss_wt, padding_mask=dec_mask)
            total_loss += batch_loss
            if batch % 10 == 0:
                print('$$$$$$$$Epoch {} Batch {} Loss {:.4f}$$$$$$$$'.format(epoch + 1, batch, batch_loss.numpy()))
        # saving (checkpoint) the model every 2 epochs
        if (epoch + 1) % save_chkp_epoch == 0:
            checkpoint.save(file_prefix=checkpoint_prefix)
        print('Epoch {} Loss {:.4f}'.format(epoch + 1, total_loss / steps_per_epoch))
        print('Time taken for 1 epoch {} sec\n'.format(time.time() - start))


def test(mode, w2v_model, model, optimizer, dataset_batch, dataset_len, dataset_oov_dict, batch_sz):

    print('dataset_len:', dataset_len)
    steps_per_epoch = dataset_len // batch_sz
    predictions = []
    targets = []
    for epoch in range(1):
        total_loss = 0
        for (batch,(enc, enc_extend, enc_mask, enc_oov_len, dec, dec_mask)) in enumerate(dataset_batch.take(steps_per_epoch)):
            # record the sample number
            st = (batch + 1) * batch_sz

            batch_loss, preds, reals = train_one_batch(mode, w2v_model, model, optimizer, dataset_oov_dict, st, enc, dec,
                                                     enc_extend, enc_mask, enc_oov_len, cov_loss_wt=0, padding_mask=dec_mask)
            total_loss += batch_loss
            predictions += preds
            targets += reals
            if batch % 10 == 0:
                print('$$$$$$$$ Batch {} Loss {:.4f}$$$$$$$$'.format(batch, batch_loss.numpy()))
        print('Epoch {} Loss {:.4f}'.format(epoch + 1, total_loss / steps_per_epoch))
        r = Rouge()
        scores = r.get_scores(predictions, targets, avg=True)
        print("\n\n")
        print(scores)


def train_test_interface(params):
    # load from local hard drive
    w2v_model = Word2Vec.load('./word2vec.model')
    embedding_matrix = np.loadtxt('embedding_matrix.txt', dtype=np.float32)

    mode = params['mode']
    max_lens = params['max_lens']  # [98, 100, 34, 103]
    batch_sz = params['batch_sz']
    test_percent = params['test_percent']
    gru_units = params['gru_units']  # 512
    att_units = params['att_units']  # 64
    learning_rate = params['learning_rate']  # 0.001
    clipvalue = params['clipvalue']  # 2.0
    checkpoint_dir = params['checkpoint_dir']  # ./train_checkpoints
    save_chkp_epoch = params['save_chkp_epoch']  # 5
    use_checkpoint = params['use_checkpoint']
    train_epoch = params['train_epoch']  # 15
    cov_loss_wt = params['cov_loss_wt']  # 0.5

    train_X = []
    train_X_oov = []
    train_y = []
    train_y_oov = []
    with open('./data/train_X_pad.txt', 'r', encoding='utf-8') as f:
        for line in f.readlines():
            train_X.append(line.strip().split(' '))
    f.close()
    with open('./data/train_X_oov.txt', 'r', encoding='utf-8') as f:
        for line in f.readlines():
            train_X_oov.append(line.strip().split(' '))
    f.close()
    with open('./data/train_y_pad.txt', 'r', encoding='utf-8') as f:
        for line in f.readlines():
            train_y.append(line.strip().split(' '))
    f.close()
    with open('./data/train_y_oov.txt', 'r', encoding='utf-8') as f:
        for line in f.readlines():
            train_y_oov.append(line.strip().split(' '))
    f.close()

    # dataset_token, dataset_extended_token, dataset_pad_mask, dataset_oov_dict, dataset_oov_len
    train_X_token, train_X_extended_token, train_X_pad_mask, train_X_oov_dict, train_X_oov_len = get_token(
        w2v_model, max_lens[1], train_X, train_X_oov)
    train_y_token, _, train_y_pad_mask, _, _ = get_token(w2v_model, max_lens[2], train_y)

    # use test_size(0.95) to adjust the train_size to accelerate experiment.
    # dataset :input, extended_input, input_pad_mask, input_oov_len, output, output_pad_mask
    dataset_train_batch, dataset_test_batch, dataset_train_len, dataset_test_len, dataset_train_oov_dict, \
    dataset_test_oov_dict = batch(batch_sz, test_percent, train_X_token, train_X_extended_token, train_X_pad_mask, train_X_oov_dict,
                                  train_X_oov_len, train_y_token, train_y_pad_mask)

    # turn on only when run example experiment
    #example_input_batch, example_enc_extend, example_enc_mask, example_oov_dict, example_oov_len, \
    #example_target_batch, example_target_mask = next(iter(dataset_train_batch))

    model = PGN(gru_units, att_units, batch_sz, embedding_matrix)
    optimizer = tf.keras.optimizers.Adam(learning_rate=learning_rate, clipvalue=clipvalue)
    checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
    checkpoint = tf.train.Checkpoint(optimizer=optimizer, encoder=model.encoder, attention=model.attention,
                                     decoder=model.decoder, pointer=model.pointer)
    if use_checkpoint:
        status = checkpoint.restore(tf.train.latest_checkpoint(checkpoint_dir))
        print('check point restored')
        # status.assert_consumed()
    if mode == 'train':
        train('train', w2v_model, model, optimizer, checkpoint, checkpoint_prefix, save_chkp_epoch,
              dataset_train_batch, dataset_train_len, dataset_train_oov_dict, batch_sz, train_epoch, cov_loss_wt)
    elif mode == 'test':
        test('test', w2v_model, model, optimizer, dataset_test_batch, dataset_test_len, dataset_test_oov_dict, batch_sz)



# def loss_function(real, pred, padding_mask):
#     #  pred [batch_sz, max_len_y, extend_vocabsize] (without argmax) real & mask [batch_sz, max_len_y]
#     #mask = tf.math.logical_not(tf.math.equal(real, 0))
#     loss = 0
#
#     for t in range(real.shape[1]):
#         loss_ = loss_object(real[:,t], pred[:,t,:])
#         #mask = tf.cast(padding_mask[:,t], dtype=loss_.dtype)
#         #loss_ *= mask
#         # print('loss_:', loss_)
#         loss_ = tf.reduce_mean(loss_, axis=0)  # batch-wise
#         loss += loss_
#         # print('loss and loss at each time step(batch sum)', loss, loss_)
#     # tf.print("loss:",loss)
#     return loss


# def coverage_loss(attn_dists, coverages, padding_mask):
#     """
#     Calculates the coverage loss from the attention distributions.
#       Args:
#         attn_dists coverages: [max_len_y, batch_sz, max_len_x, 1]
#         padding_mask: shape (batch_size, max_len_y).
#       Returns:
#         coverage_loss: scalar
#     """
#     covlosses = []
#     # transfer attn_dists coverages to [max_len_y, batch_sz, max_len_x]
#     attn_dists = tf.squeeze(attn_dists, axis=3)
#     coverages = tf.squeeze(coverages, axis=3)
#
#     assert attn_dists.shape == coverages.shape
#     for t in range(attn_dists.shape[0]):
#         covloss_ = tf.reduce_sum(tf.minimum(attn_dists[t,:,:], coverages[t,:,:]), axis=-1) # max_len_x wise
#         covlosses.append(covloss_)
#     #covlosses = tf.stack(covlosses, 1)  # change from[max_len_y, batch_sz] to [batch_sz, max_len_y]
#     #mask = tf.cast(padding_mask, dtype=covloss_.dtype)
#     #covlosses *= mask  #covloss [batch_sz, max_len_y]
#     loss = tf.reduce_sum(tf.reduce_mean(covlosses, axis=0))  # mean loss of each time step and then sum up
#     tf.print('coverage loss(batch sum):', loss)
#     return loss

