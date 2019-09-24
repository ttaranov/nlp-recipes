# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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

# Modifications copyright © Microsoft Corporation


import os
import logging
from tqdm import tqdm, trange
import collections
import json
import math
import jsonlines

import torch
from torch.utils.data import Dataset, TensorDataset, DataLoader, RandomSampler, SequentialSampler

from pytorch_transformers.tokenization_bert import BasicTokenizer
from pytorch_transformers import BertTokenizer, XLNetTokenizer
from pytorch_transformers.tokenization_bert import whitespace_tokenize
from pytorch_transformers import AdamW, WarmupLinearSchedule

from pytorch_transformers.modeling_bert import (
    BertConfig,
    BERT_PRETRAINED_MODEL_ARCHIVE_MAP,
    BertForQuestionAnswering,
)

from pytorch_transformers.modeling_xlnet import (
    XLNetConfig,
    XLNET_PRETRAINED_MODEL_ARCHIVE_MAP,
    XLNetForQuestionAnswering,
)

from utils_nlp.common.pytorch_utils import get_device, move_to_device


MODEL_CLASS = {}
MODEL_CLASS.update({k: BertForQuestionAnswering for k in BERT_PRETRAINED_MODEL_ARCHIVE_MAP})
MODEL_CLASS.update({k: XLNetForQuestionAnswering for k in XLNET_PRETRAINED_MODEL_ARCHIVE_MAP})

CONFIG_CLASS = {}
CONFIG_CLASS.update({k: BertConfig for k in BERT_PRETRAINED_MODEL_ARCHIVE_MAP})
CONFIG_CLASS.update({k: XLNetConfig for k in XLNET_PRETRAINED_MODEL_ARCHIVE_MAP})

## TODO: import from common after merging with transformers branch
MAX_SEQ_LEN = 512

TOKENIZER_CLASS = {}
TOKENIZER_CLASS.update({k: BertTokenizer for k in BERT_PRETRAINED_MODEL_ARCHIVE_MAP})
TOKENIZER_CLASS.update({k: XLNetTokenizer for k in XLNET_PRETRAINED_MODEL_ARCHIVE_MAP})
## import this from common ends


CACHED_EXAMPLES_TRAIN_FILE = "cached_examples_train.jsonl"
CACHED_FEATURES_TRAIN_FILE = "cached_features_train.jsonl"

CACHED_EXAMPLES_TEST_FILE = "cached_examples_test.jsonl"
CACHED_FEATURES_TEST_FILE = "cached_features_test.jsonl"

logger = logging.getLogger(__name__)


def _list_supported_models():
    return list(MODEL_CLASS)


QAInput = collections.namedtuple(
    "QAInput",
    ["doc_text", "question_text", "qa_id", "is_impossible", "answer_start", "answer_text"],
)


class QADataset(Dataset):
    def __init__(
        self,
        df,
        doc_text_col,
        question_text_col,
        qa_id_col,
        is_impossible_col=None,
        answer_start_col=None,
        answer_text_col=None,
    ):

        self.df = df.copy()
        self.doc_text_col = doc_text_col
        self.question_text_col = question_text_col

        ## TODO: can this be made optional???
        ## Yes, if we make evaluate_qa takes QADataset.
        self.qa_id_col = qa_id_col

        if is_impossible_col is None:
            self.is_impossible_col = "is_impossible"
            df[self.is_impossible_col] = False
        else:
            self.is_impossible_col = is_impossible_col

        if answer_start_col is not None and answer_text_col is not None:
            self.actual_answer_available = True
        else:
            self.actual_answer_available = False
        self.answer_start_col = answer_start_col
        self.answer_text_col = answer_text_col

    def __getitem__(self, idx):
        current_item = self.df.iloc[idx, ]
        if self.actual_answer_available:
            return QAInput(
                doc_text=current_item[self.doc_text_col],
                question_text=current_item[self.question_text_col],
                qa_id=current_item[self.qa_id_col],
                is_impossible=current_item[self.is_impossible_col],
                answer_start=current_item[self.answer_start_col],
                answer_text=current_item[self.answer_text_col],
            )
        else:
            return QAInput(
                doc_text=current_item[self.doc_text_col],
                question_text=current_item[self.question_text_col],
                qa_id=current_item[self.qa_id_col],
                is_impossible=current_item[self.is_impossible_col],
                answer_start=-1,
                answer_text="",
            )

    def __len__(self):
        return self.df.shape[0]


def create_qa_dataset_from_df(
    df,
    doc_text_col,
    question_text_col,
    qa_id_col,
    is_impossible_col=None,
    answer_start_col=None,
    answer_text_col=None,
):
    return QADataset(
        df,
        doc_text_col,
        question_text_col,
        qa_id_col,
        is_impossible_col,
        answer_start_col,
        answer_text_col,
    )


def get_qa_dataloader(
    qa_dataset,
    model_name,
    is_training,
    batch_size=32,
    to_lower=False,
    max_question_length=64,
    max_seq_len=MAX_SEQ_LEN,
    doc_stride=128,
    cache_dir="./cached_qa_features",
):

    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    tokenizer_class = TOKENIZER_CLASS[model_name]
    tokenizer = tokenizer_class.from_pretrained(
        model_name, do_lower_case=to_lower, cache_dir=cache_dir
    )

    if is_training and not qa_dataset.actual_answer_available:
        raise Exception("answer_start and answer_text must be provided for training data.")

    if is_training:
        examples_file = os.path.join(cache_dir, CACHED_EXAMPLES_TRAIN_FILE)
        features_file = os.path.join(cache_dir, CACHED_FEATURES_TRAIN_FILE)
    else:
        examples_file = os.path.join(cache_dir, CACHED_EXAMPLES_TEST_FILE)
        features_file = os.path.join(cache_dir, CACHED_FEATURES_TEST_FILE)

    with jsonlines.open(examples_file, "w") as examples_writer, jsonlines.open(
        features_file, "w"
    ) as features_writer:

        unique_id_all = []
        unique_id_cur = 1000000000

        features = []
        qa_examples = []
        qa_examples_json = []
        features_json = []

        for qa_input in qa_dataset:
            qa_example_cur = _create_qa_example(qa_input, is_training=is_training)

            qa_examples.append(qa_example_cur)

            qa_examples_json.append(
                {"qa_id": qa_example_cur.qa_id, "doc_tokens": qa_example_cur.doc_tokens}
            )

            features_cur = _create_qa_features(
                qa_example_cur,
                tokenizer=tokenizer,
                unique_id=unique_id_cur,
                is_training=is_training,
                max_question_length=max_question_length,
                max_seq_len=max_seq_len,
                doc_stride=doc_stride,
            )
            features += features_cur

            for f in features_cur:
                features_json.append(
                    {
                        "qa_id": f.qa_id,
                        "unique_id": f.unique_id,
                        "tokens": f.tokens,
                        "token_to_orig_map": f.token_to_orig_map,
                        "token_is_max_context": f.token_is_max_context,
                        "paragraph_len": f.paragraph_len,
                    }
                )
                unique_id_cur = f.unique_id
                unique_id_all.append(unique_id_cur)

        examples_writer.write_all(qa_examples_json)
        features_writer.write_all(features_json)

        # TODO: maybe generalize the following code
        input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long)
        p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)

        if is_training:
            start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
            end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
            qa_dataset = TensorDataset(
                input_ids,
                input_mask,
                segment_ids,
                start_positions,
                end_positions,
                cls_index,
                p_mask,
            )
            sampler = RandomSampler(qa_dataset)
        else:
            unique_id_all = torch.tensor(unique_id_all, dtype=torch.long)
            qa_dataset = TensorDataset(
                input_ids, input_mask, segment_ids, cls_index, p_mask, unique_id_all
            )
            sampler = SequentialSampler(qa_dataset)

        dataloader = DataLoader(qa_dataset, sampler=sampler, batch_size=batch_size)
        return dataloader


QAResult_ = collections.namedtuple("QAResult", ["unique_id", "start_logits", "end_logits"])
QAResultExtended = collections.namedtuple(
    "QAResultExtended",
    [
        "unique_id",
        "start_top_log_probs",
        "start_top_index",
        "end_top_log_probs",
        "end_top_index",
        "cls_logits",
    ],
)


# create a wrapper class so that we can add docstrings
class QAResult(QAResult_):
    """
    Question answering prediction result returned by BERTQAExtractor.predict.

    Args:
        unique_id: Unique id identifying the training/testing sample. It's
            used to map the prediction result back to the QAFeatures
            during postprocessing.
        start_logits (list): List of logits for predicting each token being
            the start of the answer span.
        end__logits (list): List of logits for predicting each token being
            the end of the answer span.

    """

    pass


class AnswerExtractor:
    """
    Question answer extractor based on
    :class:`pytorch_transformers.modeling_bert.BertForQuestionAnswering`

    Args:
        language (Language, optional): The pre-trained model's language.
            The value of this argument determines which BERT model is
            used. See :class:`~utils_nlp.models.bert.common.Language`
            for details. Defaults to Language.ENGLISH.
        cache_dir (str, optional):  Location of BERT's cache directory.
            When calling the `fit` method, if `cache_model` is `True`,
            the fine-tuned model is saved to this directory. If `cache_dir`
            and `load_model_from_dir` are the same and `overwrite_model` is
            `False`, the fitted model is saved to "cache_dir/fine_tuned".
            Defaults to ".".
        load_model_from_dir (str, optional): Directory to load the model from.
            The directory must contain a model file "pytorch_model.bin" and a
            configuration file "config.json". Defaults to None.

    """

    def __init__(self, model_name, cache_dir=".", load_model_from_dir=None):

        self.model_name = model_name
        self.cache_dir = cache_dir
        self.load_model_from_dir = load_model_from_dir

        config_class = CONFIG_CLASS[self.model_name]
        model_class = MODEL_CLASS[self.model_name]

        if load_model_from_dir is None:
            config = config_class.from_pretrained(self.model_name)
            self.model = model_class.from_pretrained(self.model_name, config=config)
        else:
            logger.info("Loading cached model from {}".format(load_model_from_dir))
            config = config_class.from_pretrained(load_model_from_dir)
            self.model = model_class.from_pretrained(load_model_from_dir, config=config)

    @property
    def model_name(self):
        return self._model_name

    @model_name.setter
    def model_name(self, value):
        if value not in self.list_supported_models():
            raise ValueError(
                "Model name {} is not supported by AnswerExtractor. "
                "Call 'AnswerExtractor.list_supported_models()' to get all supported model "
                "names.".format(value)
            )

        self._model_name = value
        self._model_type = value.split("-")[0]

    @property
    def model_type(self):
        return self._model_type

    @classmethod
    def list_supported_models(cls):
        return _list_supported_models()

    def fit(
        self,
        train_dataloader,
        num_gpus=None,
        num_epochs=1,
        learning_rate=2e-5,
        warmup_proportion=None,
        max_grad_norm=1.0,
        cache_model=False,
        overwrite_model=False,
    ):
        """
        Fine-tune pre-trained BertForQuestionAnswering model.

        Args:
            features (list): List of QAFeatures containing features of
                training data. Use
                :meth:`utils_nlp.models.bert.common.Tokenizer.tokenize_qa`
                to generate training features. See
                :class:`~utils_nlp.models.bert.qa_utils.QAFeatures` for
                details of QAFeatures.
            num_gpus (int, optional): The number of GPUs to use.
                If None, all available GPUs will be used. Defaults to None.
            num_epochs (int, optional): Number of training epochs. Defaults
                to 1.
            batch_size (int, optional): Training batch size. Defaults to 32.
            learning_rate (float, optional):  Learning rate of the AdamW
                optimizer. Defaults to 2e-5.
            warmup_proportion (float, optional): Proportion of training to
                perform linear learning rate warmup for. E.g., 0.1 = 10% of
                training. Defaults to None.
            max_grad_norm (float, optional): Maximum gradient norm for gradient
                clipping. Defaults to 1.0.
            cache_model (bool, optional): Whether to save the fine-tuned
                model to the `cache_dir` of the answer extractor.
                If `cache_dir` and `load_model_from_dir` are the same and
                `overwrite_model` is `False`, the fitted model is saved
                to "cache_dir/fine_tuned". Defaults to False.
            overwrite_model (bool, optional): Whether to overwrite an existing model.
                If `cache_dir` and `load_model_from_dir` are the same and
                `overwrite_model` is `False`, the fitted model is saved to
                "cache_dir/fine_tuned". Defaults to False.

        """
        # tb_writer = SummaryWriter()
        device = get_device("cpu" if num_gpus == 0 or not torch.cuda.is_available() else "gpu")
        self.model = move_to_device(self.model, device, num_gpus)

        t_total = len(train_dataloader) * num_epochs

        # Prepare optimizer and schedule (linear warmup and decay)
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in self.model.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.01,
            },
            {
                "params": [
                    p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=learning_rate, eps=1e-8)

        if warmup_proportion:
            warmup_steps = t_total * warmup_proportion
        else:
            warmup_steps = 0

        scheduler = WarmupLinearSchedule(optimizer, warmup_steps=warmup_steps, t_total=t_total)

        global_step = 0
        tr_loss = 0.0
        self.model.zero_grad()
        self.model.train()
        train_iterator = trange(int(num_epochs), desc="Epoch")
        for _ in train_iterator:
            for batch in tqdm(train_dataloader, desc="Iteration", mininterval=60):
                batch = tuple(t.to(device) for t in batch)
                inputs = {
                    "input_ids": batch[0],
                    "attention_mask": batch[1],
                    "token_type_ids": batch[2],
                    "start_positions": batch[3],
                    "end_positions": batch[4],
                }

                if self.model_type in ["xlnet"]:
                    inputs.update({"cls_index": batch[5], "p_mask": batch[6]})

                outputs = self.model(**inputs)
                loss = outputs[0]  # model outputs are always tuple in pytorch-transformers

                loss = (
                    loss.mean()
                )  # mean() to average on multi-gpu parallel (not distributed) training

                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_grad_norm)

                tr_loss += loss.item()

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                self.model.zero_grad()

                global_step += 1
                logger.info(
                    " global_step = %s, average loss = %s", global_step, tr_loss / global_step
                )

        if cache_model:
            if self.cache_dir == self.load_model_from_dir and not overwrite_model:
                output_model_dir = os.path.join(self.cache_dir, "fine_tuned")
            else:
                output_model_dir = self.cache_dir

            if not os.path.exists(self.cache_dir):
                os.makedirs(self.cache_dir)
            if not os.path.exists(output_model_dir):
                os.makedirs(output_model_dir)

            logger.info("Saving model checkpoint to %s", output_model_dir)
            # Save a trained model, configuration and tokenizer using `save_pretrained()`.
            # They can then be reloaded using `from_pretrained()`
            model_to_save = (
                self.model.module if hasattr(self.model, "module") else self.model
            )  # Take care of distributed/parallel training
            model_to_save.save_pretrained(output_model_dir)
        torch.cuda.empty_cache()

    def predict(self, test_dataloader, num_gpus=None, batch_size=32):

        """
        Predicts answer start and end logits using fine-tuned
        BertForQuestionAnswering model.

        Args:
            features (list): List of QAFeatures containing features of
                testing data. Use
                :meth:`utils_nlp.models.bert.common.Tokenizer.tokenize_qa`
                to generate training features. See
                :class:`~utils_nlp.models.bert.qa_utils.QAFeatures` for
                details of QAFeatures.
            num_gpus (int, optional): The number of GPUs to use.
                If None, all available GPUs will be used. Defaults to None.
            batch_size (int, optional): Training batch size. Defaults to 32.

        Returns:
            list: List of QAResults. Each QAResult contains the unique id,
                answer start logits, and answer end logits of the tokens in
                QAFeatures.tokens of the input features. Use
                :func:`utils_nlp.models.bert.qa_utils.postprocess_answer` to
                generate the final predicted answers.
        """

        def _to_list(tensor):
            return tensor.detach().cpu().tolist()

        device = get_device("cpu" if num_gpus == 0 or not torch.cuda.is_available() else "gpu")
        self.model = move_to_device(self.model, device, num_gpus)

        # score
        self.model.eval()

        all_results = []
        for batch in tqdm(test_dataloader, desc="Evaluating"):
            batch = tuple(t.to(device) for t in batch)
            with torch.no_grad():
                inputs = {
                    "input_ids": batch[0],
                    "attention_mask": batch[1],
                    "token_type_ids": batch[2],
                }

                if self.model_type in ["xlnet"]:
                    inputs.update({"cls_index": batch[3], "p_mask": batch[4]})

                outputs = self.model(**inputs)

                unique_id_tensor = batch[5]

            for i, u_id in enumerate(unique_id_tensor):
                if self.model_type in ["xlnet"]:
                    result = QAResultExtended(
                        unique_id=u_id.item(),
                        start_top_log_probs=_to_list(outputs[0][i]),
                        start_top_index=_to_list(outputs[1][i]),
                        end_top_log_probs=_to_list(outputs[2][i]),
                        end_top_index=_to_list(outputs[3][i]),
                        cls_logits=_to_list(outputs[4][i]),
                    )
                else:
                    result = QAResult(
                        unique_id=u_id.item(),
                        start_logits=_to_list(outputs[0][i]),
                        end_logits=_to_list(outputs[1][i]),
                    )
                all_results.append(result)
        torch.cuda.empty_cache()

        return all_results


def postprocess_bert_answer(
    results,
    examples_file,
    features_file,
    do_lower_case,
    n_best_size=20,
    max_answer_length=30,
    unanswerable_exists=False,
    output_prediction_file="./qa_predictions.json",
    output_nbest_file="./nbest_predictions.json",
    output_null_log_odds_file="./null_odds.json",
    null_score_diff_threshold=0.0,
    verbose_logging=False,
):
    """
    Postprocesses start and end logits generated by
    :meth:`utils_nlp.models.bert.BERTQAExtractor.fit`.

    Args:
        results (list): List of QAResults, each QAResult contains an
            unique id, answer start logits, and answer end logits. See
            :class:`.QAResult` for more details.
        examples (list): List of QAExamples. QAExample contains the original
            document tokens that are used to generate the final predicted
            answer from the predicted the start and end positions. See
            :class:`.QAExample` for more details.
        features (list): List of QAFeatures. QAFeatures contains the mapping
            from indices in the processed token list to the original
            document tokens that are used to generate the final predicted
            answers. See :class:`.QAFeatures` for more details.
        do_lower_case (bool): Whether an uncased tokenizer was used during
            data preprocessing. This is required during answer finalization
            by comparing the predicted answer text and the original text
            span in :func:`_get_final_text`.
        n_best_size (int, optional): The number of candidates to choose from
            each QAResult to generate the final prediction. It's also the
            maximum number of n-best answers to output for each question.
            Note that the number of n-best answers can be smaller than
            `n_best_size` because some unqualified answers, e.g. answer that
            are too long, are removed.
        max_answer_length (int, optional): Maximum length of the answer.
            Defaults to 30.
        output_prediction_file (str, optional): Path of the file to save the
            predicted answers. Defaults to "./qa_predictions.json".
        output_nbest_file (str, optional): Path of the file to save the
            n-best answers. Defaults to "./nbest_predictions.json".
        unanswerable_exists (bool, optional): Whether there are unanswerable
            questions in the data. If True, the start and end logits of the
            [CLS] token, which indicate the probability of the answer being
            empty, are included in the candidate answer list.  Defaults to
            False.
        output_null_log_odds_file (str, optional): If unanswerable_exists is
            True, the score difference between empty prediction and best
            non-empty prediction are saved to this file. These scores can be
            used to find the best threshold for predicting an empty answer.
            Defaults to "./null_odds.json".
        null_score_diff_threshold (float, optional): If the score
            difference between empty prediction and best non-empty
            prediction is higher than this threshold, the final predicted
            answer is empty. Defaults to 0.0.
        verbose_logging (bool, optional): Whether to log details of answer
            postprocessing. Defaults to False.

    Returns:
        tuple: (OrderedDict, OrderedDict, OrderedDict)
            The keys of the dictionaries are the `qa_id` of the original
            :class:`.QAExample` the answers correspond to.
            The values of the first dictionary are the predicted answer
            texts in string type.
            The values of the second dictionary  are the softmax
            probabilities of the predicted answers.
            The values of the third dictionary are the n-best answers for
            each qa_id. Note that the number of n-best answers can be smaller
            than `n_best_size` because some unqualified answers,
            e.g. answers that are too long, are removed.

    """
    with jsonlines.open(examples_file) as reader:
        examples_all = list(reader.iter())

    with jsonlines.open(features_file) as reader:
        features_all = list(reader.iter())

    qa_id_to_features = collections.defaultdict(list)
    # Map unique features to the original doc-question-answer triplet
    # Each doc-question-answer triplet can have multiple features because the doc
    # could be split into multiple spans
    for f in features_all:
        qa_id_to_features[f["qa_id"]].append(f)

    unique_id_to_result = {}
    for r in results:
        unique_id_to_result[r.unique_id] = r

    all_predictions = collections.OrderedDict()
    all_probs = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    scores_diff_json = collections.OrderedDict()

    for example in examples_all:
        # get all the features belonging to the same example,
        # i.e. paragaraph/question pair.
        features = qa_id_to_features[example["qa_id"]]

        prelim_predictions = []
        # keep track of the minimum score of null start+end of position 0
        score_null = 1000000  # large and positive

        min_null_feature_index = 0  # the paragraph slice with min null score
        null_start_logit = 0  # the start logit at the slice with min null score
        null_end_logit = 0  # the end logit at the slice with min null score
        for (feature_index, f) in enumerate(features):
            result = unique_id_to_result[f["unique_id"]]
            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            # if we could have irrelevant answers, get the min score of irrelevant
            if unanswerable_exists:
                # The first element of the start end end logits is the
                # probability of predicting the [CLS] token as the start and
                # end positions of the answer, which means the answer is
                # empty.
                feature_null_score = result.start_logits[0] + result.end_logits[0]
                if feature_null_score < score_null:
                    score_null = feature_null_score
                    min_null_feature_index = feature_index
                    null_start_logit = result.start_logits[0]
                    null_end_logit = result.end_logits[0]
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(f["tokens"]):
                        continue
                    if end_index >= len(f["tokens"]):
                        continue
                    if str(start_index) not in f["token_to_orig_map"]:
                        continue
                    if str(end_index) not in f["token_to_orig_map"]:
                        continue
                    if not f["token_is_max_context"].get(str(start_index), False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index],
                        )
                    )
        if unanswerable_exists:
            prelim_predictions.append(
                _PrelimPrediction(
                    feature_index=min_null_feature_index,
                    start_index=0,
                    end_index=0,
                    start_logit=null_start_logit,
                    end_logit=null_end_logit,
                )
            )

        # Sort by the sum of the start and end logits in ascending order,
        # so that the first element is the most probable answer
        prelim_predictions = sorted(
            prelim_predictions, key=lambda x: (x.start_logit + x.end_logit), reverse=True
        )

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            f = features[pred.feature_index]
            if pred.start_index > 0:  # this is a non-null prediction
                tok_tokens = f["tokens"][pred.start_index : (pred.end_index + 1)]
                orig_doc_start = f["token_to_orig_map"][str(pred.start_index)]
                orig_doc_end = f["token_to_orig_map"][str(pred.end_index)]
                orig_tokens = example["doc_tokens"][orig_doc_start : (orig_doc_end + 1)]
                tok_text = " ".join(tok_tokens)

                # De-tokenize WordPieces that have been split off.
                tok_text = tok_text.replace(" ##", "")
                tok_text = tok_text.replace("##", "")

                # Clean whitespace
                tok_text = tok_text.strip()
                tok_text = " ".join(tok_text.split())
                orig_text = " ".join(orig_tokens)

                final_text = _get_final_text(tok_text, orig_text, do_lower_case, verbose_logging)
                if final_text in seen_predictions:
                    continue

                seen_predictions[final_text] = True
            else:
                final_text = ""
                seen_predictions[final_text] = True

            nbest.append(
                _NbestPrediction(
                    text=final_text, start_logit=pred.start_logit, end_logit=pred.end_logit
                )
            )
        # if we didn't include the empty option in the n-best, include it
        if unanswerable_exists:
            if "" not in seen_predictions:
                nbest.append(
                    _NbestPrediction(
                        text="", start_logit=null_start_logit, end_logit=null_end_logit
                    )
                )

            # In very rare edge cases we could only have single null prediction.
            # So we just create a nonce prediction in this case to avoid failure.
            if len(nbest) == 1:
                nbest.insert(0, _NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(_NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

        assert len(nbest) >= 1

        total_scores = []
        best_non_null_entry = None
        for ie, entry in enumerate(nbest):
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry:
                if entry.text:
                    best_non_null_entry = entry
                    best_non_null_entry_index = ie

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

            if entry.text == "":
                null_prediction_index = i

        assert len(nbest_json) >= 1

        if not unanswerable_exists:
            all_predictions[example["qa_id"]] = nbest_json[0]["text"]
            all_probs[example["qa_id"]] = nbest_json[0]["probability"]
        else:
            # predict "" iff the null score - the score of best non-null > threshold
            score_diff = (
                score_null - best_non_null_entry.start_logit - (best_non_null_entry.end_logit)
            )
            scores_diff_json[example["qa_id"]] = score_diff
            if score_diff > null_score_diff_threshold:
                all_predictions[example["qa_id"]] = ""
                ## TODO: double check this
                all_probs[example["qa_id"]] = probs[null_prediction_index]
            else:
                all_predictions[example["qa_id"]] = best_non_null_entry.text
                all_probs[example["qa_id"]] = probs[best_non_null_entry_index]
        all_nbest_json[example["qa_id"]] = nbest_json

    """Write final predictions to the json file and log-odds of null if needed."""
    logger.info("Writing predictions to: %s" % (output_prediction_file))
    logger.info("Writing nbest to: %s" % (output_nbest_file))

    with open(output_prediction_file, "w") as writer:
        writer.write(json.dumps(all_predictions, indent=4) + "\n")

    with open(output_nbest_file, "w") as writer:
        writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

    if unanswerable_exists:
        logger.info("Writing null odds to: %s" % (output_null_log_odds_file))
        with open(output_null_log_odds_file, "w") as writer:
            writer.write(json.dumps(scores_diff_json, indent=4) + "\n")

    return all_predictions, all_probs, all_nbest_json


def postprocess_xlnet_answer(
    results,
    examples_file,
    features_file,
    model_name,
    do_lower_case,
    n_best_size=20,
    max_answer_length=30,
    unanswerable_exists=False,
    output_prediction_file="./qa_predictions.json",
    output_nbest_file="./nbest_predictions.json",
    output_null_log_odds_file="./null_odds.json",
    null_score_diff_threshold=0.0,
    verbose_logging=False,
):
    with jsonlines.open(examples_file) as reader:
        examples_all = list(reader.iter())

    with jsonlines.open(features_file) as reader:
        features_all = list(reader.iter())

    tokenizer = XLNetTokenizer.from_pretrained(model_name, do_lower_case=do_lower_case)

    qa_id_to_features = collections.defaultdict(list)
    # Map unique features to the original doc-question-answer triplet
    # Each doc-question-answer triplet can have multiple features because the doc
    # could be split into multiple spans
    for f in features_all:
        qa_id_to_features[f["qa_id"]].append(f)

    unique_id_to_result = {}
    for r in results:
        unique_id_to_result[r.unique_id] = r

    all_predictions = collections.OrderedDict()
    all_probs = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    scores_diff_json = collections.OrderedDict()

    for example in examples_all:
        features = qa_id_to_features[example["qa_id"]]

        prelim_predictions = []
        # keep track of the minimum score of null start+end of position 0
        score_null = 1000000  # large and positive

        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature["unique_id"]]

            cur_null_score = result.cls_logits

            # if we could have irrelevant answers, get the min score of irrelevant
            score_null = min(score_null, cur_null_score)

            for i in range(n_best_size):
                for j in range(n_best_size):
                    start_log_prob = result.start_top_log_probs[i]
                    start_index = result.start_top_index[i]

                    j_index = i * n_best_size + j

                    end_log_prob = result.end_top_log_probs[j_index]
                    end_index = result.end_top_index[j_index]

                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= feature["paragraph_len"] - 1:
                        continue
                    if end_index >= feature["paragraph_len"] - 1:
                        continue

                    if not feature["token_is_max_context"].get(str(start_index), False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue

                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=start_log_prob,
                            end_logit=end_log_prob,
                        )
                    )

        prelim_predictions = sorted(
            prelim_predictions, key=lambda x: (x.start_logit + x.end_logit), reverse=True
        )

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]

            # XLNet un-tokenizer
            # Let's keep it simple for now and see if we need all this later.
            #
            # tok_start_to_orig_index = feature.tok_start_to_orig_index
            # tok_end_to_orig_index = feature.tok_end_to_orig_index
            # start_orig_pos = tok_start_to_orig_index[pred.start_index]
            # end_orig_pos = tok_end_to_orig_index[pred.end_index]
            # paragraph_text = example.paragraph_text
            # final_text = paragraph_text[start_orig_pos: end_orig_pos + 1].strip()

            # Previously used Bert untokenizer
            tok_tokens = feature["tokens"][pred.start_index : (pred.end_index + 1)]
            orig_doc_start = feature["token_to_orig_map"][str(pred.start_index)]
            orig_doc_end = feature["token_to_orig_map"][str(pred.end_index)]
            orig_tokens = example["doc_tokens"][orig_doc_start : (orig_doc_end + 1)]
            tok_text = tokenizer.convert_tokens_to_string(tok_tokens)

            # Clean whitespace
            tok_text = tok_text.strip()
            tok_text = " ".join(tok_text.split())
            orig_text = " ".join(orig_tokens)

            final_text = _get_final_text(
                tok_text, orig_text, tokenizer.do_lower_case, verbose_logging
            )

            if final_text in seen_predictions:
                continue

            seen_predictions[final_text] = True

            nbest.append(
                _NbestPrediction(
                    text=final_text, start_logit=pred.start_logit, end_logit=pred.end_logit
                )
            )

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(_NbestPrediction(text="", start_logit=-1e6, end_logit=-1e6))

        total_scores = []
        best_non_null_entry = None
        for ie, entry in enumerate(nbest):
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry:
                best_non_null_entry = entry
                best_non_null_entry_index = ie

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

        assert len(nbest_json) >= 1
        assert best_non_null_entry is not None

        score_diff = score_null
        scores_diff_json[example["qa_id"]] = score_diff
        # note(zhiliny): always predict best_non_null_entry
        # and the evaluation script will search for the best threshold
        all_predictions[example["qa_id"]] = best_non_null_entry.text

        all_probs[example["qa_id"]] = probs[best_non_null_entry_index]

        all_nbest_json[example["qa_id"]] = nbest_json

    """Write final predictions to the json file and log-odds of null if needed."""
    logger.info("Writing predictions to: %s" % (output_prediction_file))
    logger.info("Writing nbest to: %s" % (output_nbest_file))

    with open(output_prediction_file, "w") as writer:
        writer.write(json.dumps(all_predictions, indent=4) + "\n")

    with open(output_nbest_file, "w") as writer:
        writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

    if unanswerable_exists:
        logger.info("Writing null odds to: %s" % (output_null_log_odds_file))
        with open(output_null_log_odds_file, "w") as writer:
            writer.write(json.dumps(scores_diff_json, indent=4) + "\n")

    return all_predictions, all_probs, all_nbest_json


# -------------------------------------------------------------------------------------------------
# Preprocessing helper functions
def _is_iterable_but_not_string(obj):
    """Check whether obj is a non-string Iterable."""
    return isinstance(obj, collections.Iterable) and not isinstance(obj, str)


def _create_qa_example(qa_input, is_training):

    # A data structure representing an unique document-question-answer triplet.

    # Args:
    #     qa_id (int): An unique id identifying the document-question pair.
    #         This is used to map prediction results to ground truth answers
    #         during evaluation, because the data order is not preserved
    #         during pre-processing and post-processing.
    #     doc_tokens (list): White-space tokenized tokens of the document
    #         text. This is used to generate the final answer based on
    #         predicted start and end token indices during post-processing.
    #     question_text (str): Text of the question.
    #     orig_answer_text (str): Text of the ground truth answer if available.
    #     start_position (int): Index of the starting token of the answer
    #         span, if available.
    #     end_position (int): Index of the ending token of the answer span,
    #         if available.
    #     is_impossible (bool): If the question is impossible to answer based
    #         on the given document.
    _QAExample = collections.namedtuple(
        "_QAExample",
        [
            "qa_id",
            "doc_tokens",
            "question_text",
            "orig_answer_text",
            "start_position",
            "end_position",
            "is_impossible",
        ],
    )

    def _is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
            return True
        return False

    d_text = qa_input.doc_text
    q_text = qa_input.question_text
    a_start = qa_input.answer_start
    a_text = qa_input.answer_text
    q_id = qa_input.qa_id
    impossible = qa_input.is_impossible

    d_tokens = []
    char_to_word_offset = []
    prev_is_whitespace = True
    for c in d_text:
        if _is_whitespace(c):
            prev_is_whitespace = True
        else:
            if prev_is_whitespace:
                d_tokens.append(c)
            else:
                d_tokens[-1] += c
            prev_is_whitespace = False
        char_to_word_offset.append(len(d_tokens) - 1)

    if _is_iterable_but_not_string(a_start):
        if len(a_start) != 1 and is_training and not impossible:
            raise Exception("For training, each question should have exactly 1 answer.")
        a_start = a_start[0]
        a_text = a_text[0]

    start_position = None
    end_position = None
    if is_training:
        if not impossible:
            answer_length = len(a_text)
            start_position = char_to_word_offset[a_start]
            end_position = char_to_word_offset[a_start + answer_length - 1]
            # Only add answers where the text can be exactly recovered from the
            # document. If this CAN'T happen it's likely due to weird Unicode
            # stuff so we will just skip the example.
            #
            # Note that this means for training mode, every example is NOT
            # guaranteed to be preserved.
            actual_text = " ".join(d_tokens[start_position : (end_position + 1)])
            cleaned_answer_text = " ".join(whitespace_tokenize(a_text))
            if actual_text.find(cleaned_answer_text) == -1:
                logger.warning(
                    "Could not find answer: '%s' vs. '%s'", actual_text, cleaned_answer_text
                )
                return
        else:
            start_position = -1
            end_position = -1

    return _QAExample(
        qa_id=q_id,
        doc_tokens=d_tokens,
        question_text=q_text,
        orig_answer_text=a_text,
        start_position=start_position,
        end_position=end_position,
        is_impossible=impossible,
    )


def _create_qa_features(
    example, tokenizer, unique_id, is_training, max_question_length, max_seq_len, doc_stride
):

    # BERT-format features of an unique document span-question-answer triplet.

    # Args:
    #     unique_id (int): An unique id identifying the
    #         document-question-answer triplet.
    #     example_index (int): Index of the unique QAExample from which this
    #         feature instance is derived from. A single QAExample can derive
    #         multiple QAFeatures if the document is too long and gets split
    #         into multiple document spans. This index is used to group
    #         QAResults belonging to the same document-question pair and
    #         generate the final answer.
    #     tokens (list): Concatenated question tokens and paragraph tokens.
    #     token_to_orig_map (dict): A dictionary mapping token indices in the
    #         document span back to the token indices in the original document
    #         before document splitting.
    #         This is needed during post-processing to generate the final
    #         predicted answer.
    #     token_is_max_context (list): List of booleans indicating whether a
    #         token has the maximum context in teh current document span if it
    #         appears in multiple document spans and gets multiple predicted
    #         scores. We only want to consider the score with "maximum context".
    #         "Maximum context" is defined as the *minimum* of its left and
    #         right context.
    #         For example:
    #             Doc: the man went to the store and bought a gallon of milk
    #             Span A: the man went to the
    #             Span B: to the store and bought
    #             Span C: and bought a gallon of

    #         In the example the maximum context for 'bought' would be span C
    #         since it has 1 left context and 3 right context, while span B
    #         has 4 left context and 0 right context.
    #         This is needed during post-processing to generate the final
    #         predicted answer.
    #     input_ids (list): List of numerical token indices corresponding to
    #         the tokens.
    #     input_mask (list): List of 1s and 0s indicating if a token is from
    #         the input data or padded to conform to the maximum sequence
    #         length. 1 for actual token and 0 for padded token.
    #     segment_ids (list): List of 0s and 1s indicating if a token is from
    #         the question text (0) or paragraph text (1).
    #     start_position (int): Index of the starting token of the answer span.
    #     end_position (int): Index of the ending token of the answer span.

    _QAFeatures = collections.namedtuple(
        "_QAFeatures",
        [
            "unique_id",
            "qa_id",
            "tokens",
            "token_to_orig_map",
            "token_is_max_context",
            "input_ids",
            "input_mask",
            "segment_ids",
            "start_position",
            "end_position",
            "cls_index",
            "p_mask",
            "paragraph_len",
        ],
    )

    def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer, orig_answer_text):
        """Returns tokenized answer spans that better match the annotated answer."""

        # We first project character-based annotations to
        # whitespace-tokenized words. But then after WordPiece tokenization, we can
        # often find a "better match". For example:
        #
        #   Question: What year was John Smith born?
        #   Context: The leader was John Smith (1895-1943).
        #   Answer: 1895
        #
        # The original whitespace-tokenized answer will be "(1895-1943).". However
        # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
        # the exact answer, 1895.
        #
        # However, this is not always possible. Consider the following:
        #
        #   Question: What country is the top exporter of electornics?
        #   Context: The Japanese electronics industry is the lagest in the world.
        #   Answer: Japan
        #
        # In this case, the annotator chose "Japan" as a character sub-span of
        # the word "Japanese". Since our WordPiece tokenizer does not split
        # "Japanese", we just use "Japanese" as the annotation. This is fairly rare,
        # but does happen.
        tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

        for new_start in range(input_start, input_end + 1):
            for new_end in range(input_end, new_start - 1, -1):
                text_span = " ".join(doc_tokens[new_start : (new_end + 1)])
                if text_span == tok_answer_text:
                    return (new_start, new_end)

        return (input_start, input_end)

    def _check_is_max_context(doc_spans, cur_span_index, position):
        """Check if this is the 'max context' doc span for the token."""

        # Because of the sliding window approach taken to scoring documents, a single
        # token can appear in multiple documents. E.g.
        #  Doc: the man went to the store and bought a gallon of milk
        #  Span A: the man went to the
        #  Span B: to the store and bought
        #  Span C: and bought a gallon of
        #  ...
        #
        # Now the word 'bought' will have two scores from spans B and C. We only
        # want to consider the score with "maximum context", which we define as
        # the *minimum* of its left and right context (the *sum* of left and
        # right context will always be the same, of course).
        #
        # In the example the maximum context for 'bought' would be span C since
        # it has 1 left context and 3 right context, while span B has 4 left context
        # and 0 right context.
        best_score = None
        best_span_index = None
        for (span_index, doc_span) in enumerate(doc_spans):
            end = doc_span.start + doc_span.length - 1
            if position < doc_span.start:
                continue
            if position > end:
                continue
            num_left_context = position - doc_span.start
            num_right_context = end - position
            score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
            if best_score is None or score > best_score:
                best_score = score
                best_span_index = span_index

        return cur_span_index == best_span_index

    cls_token = "[CLS]"
    sep_token = "[SEP]"
    pad_token = 0
    sequence_a_segment_id = 0
    sequence_b_segment_id = 1
    cls_token_segment_id = 0
    pad_token_segment_id = 0
    cls_token_at_end = False
    mask_padding_with_zero = True

    qa_features = []

    # unique_id identified unique feature/label pairs. It's different
    # from qa_id in that each qa_example can be broken down into
    # multiple feature samples if the paragraph length is longer than
    # maximum sequence length allowed
    query_tokens = tokenizer.tokenize(example.question_text)

    if len(query_tokens) > max_question_length:
        query_tokens = query_tokens[0:max_question_length]
    # map word-piece tokens to original tokens
    tok_to_orig_index = []
    # map original tokens to corresponding word-piece tokens
    orig_to_tok_index = []
    all_doc_tokens = []
    for (i, token) in enumerate(example.doc_tokens):
        orig_to_tok_index.append(len(all_doc_tokens))
        sub_tokens = tokenizer.tokenize(token)
        for sub_token in sub_tokens:
            tok_to_orig_index.append(i)
            all_doc_tokens.append(sub_token)

    tok_start_position = None
    tok_end_position = None
    if is_training and example.is_impossible:
        tok_start_position = -1
        tok_end_position = -1
    if is_training and not example.is_impossible:
        tok_start_position = orig_to_tok_index[example.start_position]
        if example.end_position < len(example.doc_tokens) - 1:
            # +1: move the the token after the ending token in
            # original tokens
            # -1, moves one step back
            # these two operations ensures word piece is covered
            # when it's part of the original ending token.
            tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
        else:
            tok_end_position = len(all_doc_tokens) - 1
        (tok_start_position, tok_end_position) = _improve_answer_span(
            all_doc_tokens,
            tok_start_position,
            tok_end_position,
            tokenizer,
            example.orig_answer_text,
        )

    # The -3 accounts for [CLS], [SEP] and [SEP]
    max_tokens_for_doc = max_seq_len - len(query_tokens) - 3

    # We can have documents that are longer than the maximum sequence length.
    # To deal with this we do a sliding window approach, where we take chunks
    # of the up to our max length with a stride of `doc_stride`.
    _DocSpan = collections.namedtuple("DocSpan", ["start", "length"])
    doc_spans = []
    start_offset = 0
    while start_offset < len(all_doc_tokens):
        length = len(all_doc_tokens) - start_offset
        if length > max_tokens_for_doc:
            length = max_tokens_for_doc
        doc_spans.append(_DocSpan(start=start_offset, length=length))
        if start_offset + length == len(all_doc_tokens):
            break
        start_offset += min(length, doc_stride)

    for (doc_span_index, doc_span) in enumerate(doc_spans):
        if is_training:
            unique_id += 1
        else:
            unique_id += 2

        tokens = []
        token_to_orig_map = {}
        token_is_max_context = {}
        segment_ids = []

        # p_mask: mask with 1 for token than cannot be in the answer
        # (0 for token which can be in an answer)
        # Original TF implem also keep the classification token (set to 0) (not sure why...)
        ## TODO: Should we set p_mask = 1 for cls token?
        p_mask = []

        # CLS token at the beginning
        if not cls_token_at_end:
            tokens.append(cls_token)
            segment_ids.append(cls_token_segment_id)
            p_mask.append(0)
            cls_index = 0

        # Query
        tokens += query_tokens
        segment_ids += [sequence_a_segment_id] * len(query_tokens)
        p_mask += [1] * len(query_tokens)

        # SEP token
        tokens.append(sep_token)
        segment_ids.append(sequence_a_segment_id)
        p_mask.append(1)

        # Paragraph
        for i in range(doc_span.length):
            split_token_index = doc_span.start + i
            token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

            ## TODO: maybe this can be improved to compute
            # is_max_context for each token only once.
            is_max_context = _check_is_max_context(doc_spans, doc_span_index, split_token_index)
            token_is_max_context[len(tokens)] = is_max_context
            tokens.append(all_doc_tokens[split_token_index])
            segment_ids.append(sequence_b_segment_id)
            p_mask.append(0)
        paragraph_len = doc_span.length

        # SEP token
        tokens.append(sep_token)
        segment_ids.append(sequence_b_segment_id)
        p_mask.append(1)

        # CLS token at the end
        if cls_token_at_end:
            tokens.append(cls_token)
            segment_ids.append(cls_token_segment_id)
            p_mask.append(0)
            cls_index = len(tokens) - 1  # Index of classification token

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

        # Zero-pad up to the sequence length.
        if len(input_ids) < max_seq_len:
            pad_token_length = max_seq_len - len(input_ids)
            pad_mask = 0 if mask_padding_with_zero else 1
            input_ids += [pad_token] * pad_token_length
            input_mask += [pad_mask] * pad_token_length
            segment_ids += [pad_token_segment_id] * pad_token_length
            p_mask += [1] * pad_token_length

        assert len(input_ids) == max_seq_len
        assert len(input_mask) == max_seq_len
        assert len(segment_ids) == max_seq_len

        span_is_impossible = example.is_impossible
        start_position = None
        end_position = None
        if is_training and not span_is_impossible:
            # For training, if our document chunk does not contain an annotation
            # we throw it out, since there is nothing to predict.
            doc_start = doc_span.start
            doc_end = doc_span.start + doc_span.length - 1
            out_of_span = False
            if not (tok_start_position >= doc_start and tok_end_position <= doc_end):
                out_of_span = True
            if out_of_span:
                start_position = 0
                end_position = 0
                span_is_impossible = True
            else:
                # +1 for [CLS] token
                # +1 for [SEP] toekn
                doc_offset = len(query_tokens) + 2
                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset

        if is_training and span_is_impossible:
            start_position = cls_index
            end_position = cls_index

        qa_features.append(
            _QAFeatures(
                unique_id=unique_id,
                qa_id=example.qa_id,
                tokens=tokens,
                token_to_orig_map=token_to_orig_map,
                token_is_max_context=token_is_max_context,
                input_ids=input_ids,
                input_mask=input_mask,
                segment_ids=segment_ids,
                start_position=start_position,
                end_position=end_position,
                cls_index=cls_index,
                p_mask=p_mask,
                paragraph_len=paragraph_len,
            )
        )

        return qa_features


# Preprocessing helper functions end

# -------------------------------------------------------------------------------------------------
# Post processing helper functions
_PrelimPrediction = collections.namedtuple(
    "PrelimPrediction", ["feature_index", "start_index", "end_index", "start_logit", "end_logit"]
)

_NbestPrediction = collections.namedtuple("NbestPrediction", ["text", "start_logit", "end_logit"])


def _get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the SQuAD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heuristic between
    # `pred_text` and `orig_text` to get a character-to-character alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info("Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info(
                "Length not equal after stripping spaces: '%s' vs '%s'", orig_ns_text, tok_ns_text
            )
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in tok_ns_to_s_map.items():
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position : (orig_end_position + 1)]
    return output_text


def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indexes = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indexes.append(index_and_score[i][0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs


# Post processing helper functions end
