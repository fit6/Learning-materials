import torch
import torch.nn as nn
from transformers import DistilBertModel
from transformers import BertModel
import torch.nn.functional as F
from transformers import AutoModel
import numpy as np
import time



def lengths_to_mask(lengths, max_len=None, dtype=None):
    """
    Converts a "lengths" tensor to its binary mask representation.

    Based on: https://discuss.pytorch.org/t/how-to-generate-variable-length-mask/23397

    :lengths: N-dimensional tensor
    :returns: N*max_len dimensional tensor. If max_len==None, max_len=max(lengtsh)
    """
    assert len(lengths.shape) == 1, 'Length shape should be 1 dimensional.'
    max_len = max_len or lengths.max().item()
    mask = torch.arange(
        max_len,
        device=lengths.device,
        dtype=lengths.dtype).expand(len(lengths), max_len) < lengths.unsqueeze(1)
    if dtype is not None:
        mask = torch.as_tensor(mask, dtype=dtype, device=lengths.device)
    return mask


class MaskedBatchNorm1d(nn.BatchNorm1d):
    """
    Masked verstion of the 1D Batch normalization.

    Based on: https://github.com/ptrblck/pytorch_misc/blob/20e8ea93bd458b88f921a87e2d4001a4eb753a02/batch_norm_manual.py

    Receives a N-dim tensor of sequence lengths per batch element
    along with the regular input for masking.

    Check pytorch's BatchNorm1d implementation for argument details.
    """

    def __init__(self, num_features, eps=1e-5, momentum=0.1,
                 affine=True, track_running_stats=True):
        super(MaskedBatchNorm1d, self).__init__(
            num_features,
            eps,
            momentum,
            affine,
            track_running_stats
        )

    def forward(self, inp, mask):
        self._check_input_dim(inp)

        exponential_average_factor = 0.0

        n = mask.sum()
        mask = mask / n.float()

        if self.training and self.track_running_stats:
            if self.num_batches_tracked is not None:
                self.num_batches_tracked += 1
                if self.momentum is None:  # use cumulative moving average
                    exponential_average_factor = 1.0 / float(self.num_batches_tracked)
                else:  # use exponential moving average
                    exponential_average_factor = self.momentum

        # calculate running estimates
        if self.training and n > 1:
            # Here lies the trick. Using Var(X) = E[X^2] - E[X]^2 as the biased
            # variance, we do not need to make any tensor shape manipulation.
            # mean = E[X] is simply the sum-product of our "probability" mask with the input...
            mean = (mask * inp).sum([0, 2])
            # ...whereas Var(X) is directly derived from the above formulae
            # This should be numerically equivalent to the biased sample variance
            var = (mask * inp ** 2).sum([0, 2]) - mean ** 2
            with torch.no_grad():
                self.running_mean = exponential_average_factor * mean \
                                    + (1 - exponential_average_factor) * self.running_mean
                # Update running_var with unbiased var
                self.running_var = exponential_average_factor * var * n / (n - 1) \
                                   + (1 - exponential_average_factor) * self.running_var
        else:
            mean = self.running_mean
            var = self.running_var

        inp = (inp - mean[None, :, None]) / (torch.sqrt(var[None, :, None] + self.eps))
        if self.affine:
            inp = inp * self.weight[None, :, None] + self.bias[None, :, None]

        return inp


class ModelRichContext(nn.Module):
    def __init__(self, config):
        super(ModelRichContext, self).__init__()
        self.config = config
        self.embedding_dim = config.EMBEDDING_DIM
        self.dropout = config.dropout
        # self.entity_type = len(self.config.fact_container.entity_to_ids)
        self.arg_roles = config.arg_roles

        self.pretrained_weights = str(config.pretrained_weights)
        if self.pretrained_weights == 'distilbert-base-uncased':
            self.bert = DistilBertModel.from_pretrained(self.pretrained_weights)
        elif self.pretrained_weights == 'bert':
            self.bert = BertModel.from_pretrained('./data/chinese_wwm_ext_pytorch', output_attentions=True, output_hidden_states=True)
        elif self.pretrained_weights == 'spanbert':
            self.bert = AutoModel.from_pretrained("SpanBERT/spanbert-large-cased")
        elif self.pretrained_weights == 'bertlarge':
            self.bert = BertModel.from_pretrained('bert-large-uncased', output_attentions=True, output_hidden_states=True)
        else:
            self.bert = None

        self.extra_bert = config.extra_bert
        self.use_extra_bert = config.use_extra_bert
        if self.use_extra_bert:
            self.embedding_dim *= 2
        
        self.U = nn.Parameter(torch.rand(3 * self.embedding_dim, self.embedding_dim))
        self.n_hid = 768
        self.linear = nn.Sequential(
            nn.Dropout(self.dropout),
            nn.Linear(self.embedding_dim*6, self.n_hid),
            nn.GELU(),
            nn.Dropout(self.dropout),
            nn.Linear(self.n_hid, 1)
        )
        self.sqrt_d = np.sqrt(self.embedding_dim)
        self.activation = nn.Sigmoid()

    # def get_fist_subword_embeddings(self, all_embeddings, idxs_to_collect, bert_sentence_lengths):
    #     """
    #     Pick first subword embeddings with the indices list idxs_to_collect
    #     :param all_embeddings:
    #     :param idxs_to_collect:
    #     :param targets:
    #     :param bert_sentence_lengths:
    #     :return:
    #     """
    #     sent_embeddings = []
    #     N = all_embeddings.shape[0]  # it's equivalent to N=len(all_embeddings)

    #     # Other two mode need to be taken care of the issue
    #     # that the last index becomes the [SEP] after argument types
    #     arg_type_embeddings = []
    #     bert_sentence_lengths = bert_sentence_lengths.long()
    #     for i in range(N):
    #         this_idxs_to_collect = idxs_to_collect[i]
    #         this_idxs_to_collect = this_idxs_to_collect[this_idxs_to_collect>0]
    #         collected = all_embeddings[i, this_idxs_to_collect[:-2]]  # collecting a slice of tensor
    #         sent_embeddings.append(collected)
    #         second_last_sep_index = this_idxs_to_collect[-2]

    #         # argument type embedding
    #         arg_type_embedding = all_embeddings[i, second_last_sep_index+1:bert_sentence_lengths[i]]
    #         arg_type_embeddings.append(arg_type_embedding)

    #     max_sent_len = idxs_to_collect.shape[1] - 2
    #     max_len_arg = 9

    #     for i in range(N):
    #         arg_type_embeddings[i] = torch.cat((arg_type_embeddings[i], torch.zeros(max_len_arg - len(arg_type_embeddings[i]), self.embedding_dim).cuda()))
    #         sent_embeddings[i] = torch.cat((sent_embeddings[i], torch.zeros(max_sent_len - len(sent_embeddings[i]), self.embedding_dim).cuda()))

    #     sent_embeddings = torch.stack(sent_embeddings)
    #     arg_type_embeddings = torch.stack(arg_type_embeddings)
    #     return sent_embeddings, arg_type_embeddings

    def get_fist_subword_embeddings(self, all_embeddings, idxs_to_collect_sent, idxs_to_collect_event):
        """
        Pick first subword embeddings with the indices list idxs_to_collect
        :param all_embeddings:
        :param idxs_to_collect:
        :param sentence_lengths:
        :return:
        """
        N = all_embeddings.shape[0]  # it's equivalent to N=len(all_embeddings)

        # Other two mode need to be taken care of the issue
        # that the last index becomes the [SEP] after argument types
        sent_embeddings = []
        event_embeddings = []

        for i in range(N):
            to_collect_sent, to_collect_event = idxs_to_collect_sent[i], idxs_to_collect_event[i]
            collected_sent = all_embeddings[i, torch.nonzero(to_collect_sent,as_tuple=False).squeeze(-1)]  # collecting a slice of tensor
            seq_index = torch.nonzero(to_collect_sent,as_tuple=False).squeeze(-1)[0]
            to_collect_event = to_collect_event[:seq_index]
            arg_begins_index = torch.nonzero(to_collect_event,as_tuple=False).squeeze(-1)
            arg_ends_index = []
            event_embedding = []
            for a in arg_begins_index:
                b = a+1
                while b<len(to_collect_event):
                    if to_collect_event[b] == 0:
                        b+=1
                        continue
                    else:
                        break
                arg_ends_index.append(b)
            
            for z, v in zip(arg_begins_index,arg_ends_index):
                collected_event = torch.sum(all_embeddings[i, z:v],dim=0)/(v-z)
                event_embedding.append(collected_event)
            event_embedding = torch.stack(event_embedding)
            # event_embedding = torch.Tensor([item.cpu().detach().numpy() for item in event_embedding]).cuda()
            # print(len(event_embedding))
            # print(event_embedding.shape)
            # time.sleep(10000)
            # collected_event = all_embeddings[i, torch.nonzero(to_collect_event,as_tuple=False).squeeze(-1)]  # collecting a slice of tensor
            sent_embeddings.append(collected_sent)
            event_embeddings.append(event_embedding)

        max_sent_len = torch.max(torch.sum(idxs_to_collect_sent, dim=-1))
        max_event_len = torch.max(torch.sum(idxs_to_collect_event, dim=-1))
        

        for i in range(N):
            sent_embeddings[i] = torch.cat((sent_embeddings[i], torch.zeros(max_sent_len - len(sent_embeddings[i]), self.embedding_dim).cuda()))
            event_embeddings[i] = torch.cat((event_embeddings[i], torch.zeros(max_event_len - len(event_embeddings[i]), self.embedding_dim).cuda()))
        sent_embeddings = torch.stack(sent_embeddings)
        event_embeddings = torch.stack(event_embeddings)
        return sent_embeddings, event_embeddings


    @staticmethod
    def get_trigger_embeddings(sent_embeddings, is_triggers):
        """
        Select trigger embedding with the is_trigger mask
        :param sent_embeddings:
        :param is_triggers:
        :return:
        """
        return torch.sum(sent_embeddings*is_triggers.unsqueeze(-1)/torch.sum(is_triggers, dim=1).unsqueeze(-1).unsqueeze(-1), dim=1)
    
    @staticmethod
    def get_entity_embeddings(sent_embeddings, is_triggers):
        """
        Select trigger embedding with the is_trigger mask
        :param sent_embeddings:
        :param is_triggers:
        :return:
        """
        z = []
        for i in range(sent_embeddings.shape[0]):
            z.append(torch.sum(
            sent_embeddings[i] * is_triggers[i].unsqueeze(-1) / (torch.sum(is_triggers[i], dim=1)+ 1e-6).unsqueeze(-1).unsqueeze(-1) ,
            dim=1))
        z = torch.stack(z)
        return z

    def generate_concat(self, sent_embeddings, trigger_embeddings):
        trigger_count = trigger_embeddings.shape[1]
        sent_len = sent_embeddings.shape[1]

        trigger_embeddings = torch.unsqueeze(trigger_embeddings, 2).repeat(1,1,sent_len, 1)
        sent_embeddings = torch.unsqueeze(sent_embeddings, 1).repeat(1, trigger_count, 1, 1)
        if self.config.without_trigger:
            sent_trigger_cat = torch.cat((sent_embeddings, trigger_embeddings), -1)
            return sent_trigger_cat
        else:
            return sent_embeddings

    def forward(self, sentence_batch, idxs_to_collect_event, idxs_to_collect_sent, is_triggers, bert_sentence_lengths,
                 entity_mapping, arg_mapping, entity_num):
        # get embeddings
        sent_mask = (sentence_batch != 0) * 1
        token_type_ids = sent_mask.detach().clone()
        sep_idx = (sentence_batch == 102).nonzero(as_tuple=True)[1].reshape(sentence_batch.shape[0], -1)[:, 0]
        for i in range(sentence_batch.shape[0]):
            token_type_ids[i, :sep_idx[i]+1] = 0
        all_embeddings, _, hidden_states, hidden_layer_att = self.bert(sentence_batch.long(), token_type_ids=token_type_ids, attention_mask=sent_mask)
        if self.use_extra_bert:
            extra_bert_outputs = hidden_states[self.extra_bert]
            all_embeddings = torch.cat([all_embeddings, extra_bert_outputs], dim=2)
        all_embeddings = all_embeddings * sent_mask.unsqueeze(-1)
        
        
        sent_embeddings, arg_embeddings = self.get_fist_subword_embeddings(all_embeddings, idxs_to_collect_sent, idxs_to_collect_event)
        
        # entity_embeddings = sent_embeddings.permute(0, 2, 1).matmul(entity_mapping).permute(0,2,1)
        entity_embeddings = self.get_entity_embeddings(sent_embeddings, entity_mapping)#32,16,768
        
        entity_embeddings = torch.where(torch.isnan(entity_embeddings),torch.full_like(entity_embeddings,0),entity_embeddings)
        trigger_candidates = self.get_trigger_embeddings(sent_embeddings, is_triggers)#32,768
        
        
        
        # arg_embeddings = arg_embeddings.transpose(1,2).matmul(arg_weight_matrices.float()).transpose(1, 2)
        _trigger = trigger_candidates.unsqueeze(1).repeat(1, entity_embeddings.shape[1], 1)
        entity_trigger = entity_embeddings * _trigger#torch.Size([32, 16, 768])
        H_1 = torch.cat((entity_embeddings, _trigger, entity_trigger), dim=-1)
        H_ = H_1.matmul(self.U)#torch.Size([32, 16, 768])
        # token to argument attention
        token2arg_score = torch.sum(H_.unsqueeze(2) * arg_embeddings.unsqueeze(1), dim=-1) * (1 / self.sqrt_d)#32,16,6
        

        token_argAwared = torch.matmul(F.softmax(token2arg_score, -1), arg_embeddings)
        b_weight = F.softmax(torch.max(token2arg_score, 2)[0].unsqueeze(1), -1)
        arg_tokenAwared = torch.matmul(b_weight, H_).repeat([1, arg_embeddings.shape[1], 1])

        # attention weights
        # token2arg_softmax = ((token2arg_score-5)/2).unsqueeze(-1)#32,16,6,1
        # arg2token_softmax = ((token2arg_score-5)/2).unsqueeze(-1)

        # token_argAwared = torch.sum(arg_embeddings.unsqueeze(1) * token2arg_softmax, dim=2)   # b * sent_len * 768
        # arg_tokenAwared = torch.sum(entity_embeddings.unsqueeze(2) * arg2token_softmax, dim=1)  # b *  arg_len * 768
        # bidirectional attention
        A_h2u = token_argAwared.unsqueeze(2).repeat(1,1,arg_embeddings.shape[1],1)# b * sent_len * arg_len * 768
        A_u2h = arg_tokenAwared.unsqueeze(1).repeat(1,H_.shape[1],1,1)#b * sent_len * arg_len * 768
        # argumentation embedding
        U_ = arg_embeddings.unsqueeze(1).repeat(1,H_.shape[1],1,1)

        # entity-entity attention
        # last0_layer_atten = self.select_hidden_att(hidden_layer_att[-1], idxs_to_collect_sent)
        # last1_layer_atten = self.select_hidden_att(hidden_layer_att[-2], idxs_to_collect_sent)
        # last2_layer_atten = self.select_hidden_att(hidden_layer_att[-3], idxs_to_collect_sent)
        # token2token_softmax = (last0_layer_atten + last1_layer_atten + last2_layer_atten)/3 #[32, 183, 183]
        
        token2token_score = torch.sum(H_.unsqueeze(2) * H_.unsqueeze(1), dim=-1) * (1 / self.sqrt_d)
        A_h2h = torch.matmul(F.softmax(token2token_score, -1), H_).unsqueeze(2).repeat(1, 1, arg_embeddings.shape[1], 1)
        # A_h2h = token2token_softmax.matmul(sent_embeddings).unsqueeze(2).repeat(1, 1, arg_embeddings.shape[1], 1)#[32, 183, 6,768]
        # H_ = sent_embeddings.unsqueeze(2).repeat(1, 1, arg_embeddings.shape[1],1)#[32, 183, 6,768]
        H_ = H_.unsqueeze(2).repeat(1, 1, arg_embeddings.shape[1],1)
        # entity_mapping = entity_mapping.permute(0, 2, 1)
        # entity_mapping = torch.FloatTensor(entity_mapping.cpu().detach().numpy()).cuda()
        # A_h2h = A_h2h.permute(0, 2, 3, 1).matmul(entity_mapping.unsqueeze(1)).permute(0, 3, 1, 2)#[32, 16, 6, 768]
        # H_ = H_.permute(0, 2, 3, 1).matmul(entity_mapping.unsqueeze(1)).permute(0, 3, 1, 2)#[32, 16, 6, 768]

        # arg role to arg role attention
        arg2arg_score = torch.sum(arg_embeddings.unsqueeze(2) * arg_embeddings.unsqueeze(1), dim=-1) * (1 / self.sqrt_d)
        A_u2u = torch.matmul(F.softmax(arg2arg_score, -1), arg_embeddings).unsqueeze(1).repeat(1, H_.shape[1], 1, 1)
        # arg2arg_softmax = F.softmax(arg_embeddings.matmul(arg_embeddings.transpose(-1,-2)), dim=-1)
        # A_u2u = arg2arg_softmax.matmul(arg_embeddings).unsqueeze(1).repeat(1, H_.shape[1], 1, 1)#[32, 16, 6, 768]
    
        latent = torch.cat((H_, U_, A_h2u, A_h2h, A_u2h, A_u2u), dim=-1)
        score = self.linear(latent)
        score = score.squeeze(-1)
        score = self.map_arg_to_ids(score, arg_mapping, entity_num)
        # score = self.activation(score)
        
        return score

    def map_arg_to_ids(self, score, arg_mapping, entity_num):
        """
        Here we put each argument embedding back to its original place.
        In the input [CLS] sentence [SEP] arguments [SEP],
        arguments contains arguments of the specific trigger type.
        Thus we need to put them back to their actual indices
        :param score:
        :param arg_mapping:
        :return:
        """
        b, s, _ = score.shape
        d = self.arg_roles+1
        new_score = -1e6 * torch.ones(b, s, d).cuda()
        for i in range(b):
            ids = arg_mapping[i][arg_mapping[i] < self.arg_roles+1]
            new_score[i, :entity_num[i], ids] = score[i, :entity_num[i], :len(ids)]
        return new_score#([32, 16, 122])

    @staticmethod
    def select_hidden_att(hidden_att, ids_to_collect):
        """
        Pick attentions from hidden layers
        :param hidden_att: of dimension (batch_size, embed_length, embed_length)
        :return:
        """
        N = hidden_att.shape[0]
        # sent_len = ids_to_collect.shape[1] - 2
        sent_len = torch.max(torch.sum(ids_to_collect, dim=-1))
        hidden_att = torch.mean(hidden_att, 1)
        hidden_att_selected = torch.zeros(N, sent_len, sent_len).cuda()

        for i in range(N):
            to_collect = ids_to_collect[i]
            # to_collect = to_collect[to_collect>0][:-2]
            to_collect = torch.nonzero(to_collect, as_tuple=False).squeeze(-1)
            collected = hidden_att[i, to_collect][:,to_collect]  # collecting a slice of tensor
            hidden_att_selected[i, :len(to_collect), :len(to_collect)] = collected

        return hidden_att_selected/(torch.sum(hidden_att_selected, dim=-1, keepdim=True)+1e-9)