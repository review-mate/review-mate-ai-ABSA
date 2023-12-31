import json
import os
import torch
import torch.nn as nn
from tqdm import trange
from torch.utils.data import DataLoader, TensorDataset
from transformers import XLMRobertaModel, AutoTokenizer
from transformers import get_linear_schedule_with_warmup
from transformers import AdamW
from datasets import load_metric
from sklearn.metrics import f1_score
import pandas as pd
import copy
import sagemaker
import boto3
from smart_open import open

def set_bucket(custom_bucket_name = "bucket-sagemaker-test3-230911"):
    # 새로운 SageMaker 세션을 생성하고 사용자 지정 버킷을 설정합니다.
    sagemaker_session = sagemaker.Session(default_bucket = custom_bucket_name)

    # 변경된 버킷 이름을 가져옵니다.
    new_bucket_name = sagemaker_session.default_bucket()
    print("새로 설정된 S3 버킷 이름:", new_bucket_name)
    
    return sagemaker_session, new_bucket_name

def jsonlload(fname, encoding="utf-8"):
    json_list = []
    with open(fname, 'rb') as s3_file:
        for line in s3_file:
            json_list.append(json.loads(line.decode('utf-8')))
    return json_list

class SimpleClassifier(nn.Module):
    
    def __init__(self, num_label):
        super().__init__()
        classifier_hidden_size = 768
        classifier_dropout_prob = 0.1
        self.dense = nn.Linear(classifier_hidden_size, classifier_hidden_size)
        self.dropout = nn.Dropout(classifier_dropout_prob)
        self.output = nn.Linear(classifier_hidden_size, num_label)

    def forward(self, features):
        x = features[:, 0, :]
        x = self.dropout(x)
        x = self.dense(x)
        x = torch.tanh(x)
        x = self.dropout(x)
        x = self.output(x)
        return x


class RoBertaBaseClassifier(nn.Module):
    def __init__(self, num_label, base_model, len_tokenizer):
        super(RoBertaBaseClassifier, self).__init__()

        self.num_label = num_label
        self.xlm_roberta = XLMRobertaModel.from_pretrained(base_model)
        self.xlm_roberta.resize_token_embeddings(len_tokenizer)

        self.labels_classifier = SimpleClassifier(self.num_label)

    def forward(self, input_ids, attention_mask, labels=None):
        outputs = self.xlm_roberta(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=None
        )

        sequence_output = outputs[0]
        logits = self.labels_classifier(sequence_output)

        loss = None

        if labels is not None:
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_label),
                                                labels.view(-1))

        return loss, logits

def predict_from_korean_form(device, tokenizer, ce_model, pc_model, data, entity_property_pair, tf_id_to_name, polarity_id_to_name):
    ce_model.to(device)
    ce_model.eval()
    for sentence in data:
        form = sentence['sentence_form']
        sentence['annotation'] = []
        if type(form) != str:
            print("form type is arong: ", form)
            continue
        for pair in entity_property_pair:
            tokenized_data = tokenizer(form, pair, padding='max_length', max_length=256, truncation=True)
            input_ids = torch.tensor([tokenized_data['input_ids']]).to(device)
            attention_mask = torch.tensor([tokenized_data['attention_mask']]).to(device)
            with torch.no_grad():
                _, ce_logits = ce_model(input_ids, attention_mask)
            #print(ce_model(input_ids, attention_mask))
            ce_predictions = torch.argmax(ce_logits, dim = -1)
            #print("ce_pred : ", ce_predictions, pair)
            ce_result = tf_id_to_name[ce_predictions[0]]

            if ce_result == 'True':
                with torch.no_grad():
                    _, pc_logits = pc_model(input_ids, attention_mask)

                pc_predictions = torch.argmax(pc_logits, dim=-1)
                pc_result = polarity_id_to_name[pc_predictions[0]]

                sentence['annotation'].append([pair, pc_result])
    return data

def inference():
    sagemaker_session, new_bucket_name = set_bucket()
    ROOT = 's3://' + new_bucket_name
    test_data_path = ROOT + '/korean_baseline/data/sample.jsonl'

    max_len = 256
    base_model = 'xlm-roberta-base'

    eps = 1e-8
    num_train_epochs = 20

    entity_property_pair = ['친절도', '청결', '편의시설', '기타']
    tf_id_to_name = ['True', 'False']
    tf_name_to_id = {tf_id_to_name[i]: i for i in range(len(tf_id_to_name))}

    polarity_id_to_name = ['positive', 'negative', 'neutral']
    polarity_name_to_id = {polarity_id_to_name[i]: i for i in range(len(polarity_id_to_name))}

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    special_tokens_dict = {
        'additional_special_tokens': ['&name&', '&affiliation&', '&social-security-num&', '&tel-num&', '&card-num&', '&bank-account&', '&num&', '&online-account&']
    }

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    num_added_toks = tokenizer.add_special_tokens(special_tokens_dict)
    test_data = jsonlload(test_data_path)

    model = RoBertaBaseClassifier(len(tf_id_to_name), base_model, len(tokenizer))  
    local_file_path = 'entity_property_model.pt'
    checkpoint = torch.load(local_file_path, map_location=torch.device('cpu'))
    model.load_state_dict(checkpoint, strict=False)

    local_file_path = 'polarity_model.pt'
    polarity_model = RoBertaBaseClassifier(len(polarity_id_to_name), base_model, len(tokenizer))
    checkpoint = torch.load(local_file_path, map_location=torch.device('cpu'))

    polarity_model.load_state_dict(checkpoint, strict=False)

    #print(mode, polarity_model)
    pred_data = predict_from_korean_form(device, tokenizer, model, polarity_model, copy.deepcopy(test_data), entity_property_pair, tf_id_to_name, polarity_id_to_name)
    return pred_data

if __name__ == '__main__':
    inference()