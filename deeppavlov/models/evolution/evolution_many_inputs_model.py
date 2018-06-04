"""
Copyright 2017 Neural Networks and Deep Learning lab, MIPT

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import numpy as np
from copy import copy, deepcopy
from keras.layers import Dense, Input, concatenate, Activation
from keras.layers.convolutional import Conv1D
from keras.layers.core import Dropout
from keras.layers.normalization import BatchNormalization
from keras.layers.pooling import GlobalMaxPooling1D, MaxPooling1D
from keras.layers.recurrent import LSTM
from keras.layers.wrappers import Bidirectional
from keras.models import Model
from keras.regularizers import l2
from keras.layers import Concatenate, Reshape, CuDNNLSTM, Lambda, Add, Subtract, Multiply
from keras import backend as K
from overrides import overrides
from pathlib import Path

from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.keras_model import KerasModel
from deeppavlov.models.classifiers.intents.intent_model import KerasIntentModel
from deeppavlov.models.classifiers.intents.utils import labels2onehot, log_metrics, proba2labels
from deeppavlov.models.embedders.fasttext_embedder import FasttextEmbedder
from deeppavlov.models.classifiers.intents.utils import md5_hashsum
from deeppavlov.models.tokenizers.nltk_tokenizer import NLTKTokenizer
from deeppavlov.core.common.log import get_logger
from deeppavlov.models.evolution.check_binary_mask import number_to_type_layer, \
    find_sources_and_sinks, get_digraph_from_binary_mask, get_graph_and_plot
from deeppavlov.models.evolution.utils import expand_tile
from deeppavlov.core.common.file import save_json, read_json
from deeppavlov.core.layers.keras_layers import multiplicative_self_attention_init, \
    multiplicative_self_attention_get_output


log = get_logger(__name__)


@register('evolution_many_inputs_classification_model')
class KerasEvolutionClassificationManyInputsModel(KerasIntentModel):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.opt["binary_mask"] = np.array(self.opt["binary_mask"])
        get_graph_and_plot(self.opt["nodes"], self.opt["binary_mask"], self.opt["n_types"],
                           path=str(self.save_path.resolve().parent))

    def texts2vec(self, sentences, i):
        """
        Convert texts to vector representations using embedder and padding up to self.opt["text_size"] tokens
        Args:
            sentences: list of lists of tokens

        Returns:
            array of embedded texts
        """
        pad = np.zeros(self.opt['embedding_size'])
        if type(self.opt['text_size']) is list:
            text_size = self.opt['text_size'][i]
        else:
            text_size = self.opt['text_size']
        embeddings_batch = self.fasttext_model([sen[:text_size] for sen in sentences])
        embeddings_batch = [[pad] * (text_size - len(tokens)) + tokens for tokens in embeddings_batch]

        embeddings_batch = np.asarray(embeddings_batch)
        return embeddings_batch

    @overrides
    def train_on_batch(self, *args, **kwargs):
        """
        Train the model on the given batch
        Args:
            texts - list of texts (or list of lists of text tokens)
            labels - list of labels

        Returns:
            loss and metrics values on the given batch
        """
        if len(args) > len(self.opt["in"]):
            labels = args[-1]
            texts = args[:-1]
        else:
            labels = None
            texts = args

        features = []
        for i in range(len(self.opt["in"])):
            if isinstance(texts[i][0], str):
                features.append(self.texts2vec(self.tokenizer(list(texts[i])), i))
            else:
                features.append(self.texts2vec(list(texts[i]), i))

        onehot_labels = labels2onehot(labels, classes=self.classes)
        metrics_values = self.model.train_on_batch(features, onehot_labels)
        return metrics_values

    @overrides
    def infer_on_batch(self, *args, **kwargs):
        """
        Infer the model on the given batch
        Args:
            texts - list of texts (or list of lists of text tokens)
            labels - list of labels

        Returns:
            loss and metrics values on the given batch, if labels are given
            predictions, otherwise
        """
        if len(args) > 1:
            labels = args[-1]
            texts = args[:-1]
        elif len(args) == 1:
            labels = None
            texts = args[0]
        else:
            raise ValueError("Nothing to infer in infer_on_batch")

        features = []
        for i in range(len(self.opt["in"])):
            if isinstance(texts[i][0], str):
                features.append(self.texts2vec(self.tokenizer(list(texts[i])), i))
            else:
                features.append(self.texts2vec(list(texts[i]), i))

        if labels:
            onehot_labels = labels2onehot(labels, classes=self.classes)
            metrics_values = self.model.test_on_batch(features, onehot_labels)
            return metrics_values
        else:
            predictions = self.model.predict(features)
            return predictions

    @overrides
    def __call__(self, *args, **kwargs):
        """
        Infer on the given data
        Args:
            data: [list of sentences]
            *args:

        Returns:
            for each sentence:
                vector of probabilities to belong with each class
                or list of labels sentence belongs with
        """
        assert len(args) == len(self.opt["in"])
        preds = np.array(self.infer_on_batch(args))

        labels = proba2labels(preds, confident_threshold=self.opt['confident_threshold'], classes=self.classes)
        return labels, [dict(zip(self.classes, preds[i])) for i in range(preds.shape[0])]

    def get_node_output(self, model_layers, node_str_id, dg, params, edges_outputs=None, inp=None):
        if inp is None:
            input_nodes = [edge[0] for edge in dg.in_edges(node_str_id)]
            inp_list = []
            for input_node in input_nodes:
                if len(K.int_shape(edges_outputs[input_node])) == 3:
                    inp_list.append(edges_outputs[input_node])
                elif len(K.int_shape(edges_outputs[input_node])) == 2:
                    input_expanded = Lambda(lambda x: expand_tile(x, axis=1))(edges_outputs[input_node])
                    inp_list.append(input_expanded)
                else:
                    raise ValueError("All the layers should take in and take out 2 and 3 dimensional tensors!")
            if len(input_nodes) > 1:
                try:
                    inp = Concatenate()(inp_list)
                except ValueError:
                    time_steps = []
                    features = []
                    for i in range(len(inp_list)):
                        if len(K.int_shape(inp_list[i])) == 2:
                            inp_list[i] = Lambda(lambda x: expand_tile(x, axis=1))(inp_list[i])
                        time_steps.append(K.int_shape(inp_list[i])[1])
                        features.append(K.int_shape(inp_list[i])[2])
                    new_feature_shape = max(features)
                    new_inp_list = []
                    for i in range(len(inp_list)):
                        if K.int_shape(inp_list[i])[2] == new_feature_shape:
                            new_inp_list.append(inp_list[i])
                        else:
                            new_inp_list.append(Dense(new_feature_shape)(inp_list[i]))
                    inp = Concatenate(axis=1)(new_inp_list)
            else:
                inp = inp_list[0]

        if params[params["nodes"][node_str_id]]["node_name"] == "SelfMultiplicativeAttention":
            node_params = deepcopy(params[params["nodes"][node_str_id]])
            node_params.pop("node_name")
            node_params.pop("node_type")
            node_params.pop("node_layer")
            output_of_node = multiplicative_self_attention_get_output(inp,
                                                                      model_layers[params["nodes"][node_str_id]])
        else:
            node_func = globals().get(params[params["nodes"][node_str_id]]["node_name"], None)
            node_params = deepcopy(params[params["nodes"][node_str_id]])
            node_params.pop("node_name")
            node_params.pop("node_type")
            node_params.pop("node_layer")
            output_of_node = model_layers[params["nodes"][node_str_id]](inp)
        return output_of_node

    def initialize_all_nodes(self, params):
        dg = get_digraph_from_binary_mask(params["nodes"], np.array(params["binary_mask"]))
        sources, sinks, isolates = find_sources_and_sinks(dg)

        model_layers = {}
        for node_str_id in list(params["nodes"].keys()):
            if not(node_str_id in isolates):
                if params[params["nodes"][node_str_id]]["node_name"] == "BiCuDNNLSTM":
                    node_params = deepcopy(params[params["nodes"][node_str_id]])
                    node_params.pop("node_name")
                    node_params.pop("node_type")
                    node_params.pop("node_layer")
                    model_layers[params["nodes"][node_str_id]] = Bidirectional(CuDNNLSTM(**node_params))
                elif params[params["nodes"][node_str_id]]["node_name"] == "SelfMultiplicativeAttention":
                    node_params = deepcopy(params[params["nodes"][node_str_id]])
                    node_params.pop("node_name")
                    node_params.pop("node_type")
                    node_params.pop("node_layer")
                    model_layers[params["nodes"][node_str_id]] = \
                        multiplicative_self_attention_init(**node_params)
                else:
                    node_func = globals().get(params[params["nodes"][node_str_id]]["node_name"], None)
                    node_params = deepcopy(params[params["nodes"][node_str_id]])
                    node_params.pop("node_name")
                    node_params.pop("node_type")
                    node_params.pop("node_layer")
                    if callable(node_func):
                        model_layers[params["nodes"][node_str_id]] = node_func(**node_params)
                    else:
                        raise AttributeError("Node {} is not defined correctly".format(node_str_id))

        return model_layers

    def evolution_many_inputs_classification_model(self, params):
        """
        Build un-compiled model of shallow-and-wide CNN
        Args:
            params: dictionary of parameters for NN

        Returns:
            Un-compiled model
        """
        inputs = []
        if type(params['text_size']) is list:
            for i in range(len(params["in"])):
                inputs.append(Input(shape=(params['text_size'][i], params['embedding_size'])))
        else:
            for i in range(len(params["in"])):
                inputs.append(Input(shape=(params['text_size'], params['embedding_size'])))

        full_outputs = []

        if np.sum(params["binary_mask"]) == 0:
            dense1 = Dense(1, activation=None)
            globalmaxpooling = GlobalMaxPooling1D()
            for inp in inputs:
                output = dense1(inp)
                full_outputs.append(globalmaxpooling(output))

            summ = Add()(full_outputs)
            mult = Multiply()(full_outputs)

            try:
                subt = Subtract()(full_outputs)
                full_outputs.append(subt)
            except ValueError:
                pass
            full_outputs.append(summ)
            full_outputs.append(mult)

            output = Concatenate()(full_outputs)
            output = Dense(self.n_classes, activation=None)(output)
            activation = params.get("last_layer_activation", "sigmoid")
            act_output = Activation(activation)(output)
            model = Model(inputs=inputs, outputs=act_output)
            return model

        model_layers = self.initialize_all_nodes(params)

        for inp in inputs:
            dg = get_digraph_from_binary_mask(params["nodes"], np.array(params["binary_mask"]))
            sources, sinks, isolates = find_sources_and_sinks(dg)

            edges_outputs = {}

            # sequence_of_nodes is a list of lists.
            # each element of sequence_of_nodes is a list that contains nodes (keras layers)
            # that could be initialized when all nodes from previous lists are initialized
            sequence_of_nodes = [sources]

            while True:
                if set(sinks).issubset(set(sum(sequence_of_nodes, []))):
                    break
                next_nodes = []
                # want to get list of nodes that can be initialized next
                for node_str_id in sequence_of_nodes[-1]:
                    # for each node that were initialized on the previous step
                    # take output edges
                    out_edges = dg.out_edges(node_str_id)
                    for edge in out_edges:
                        # for all output edge
                        # collect nodes that are input nodes
                        # for considered child of node_str_id (edge[1])
                        in_nodes_to_edge = [in_edge[0] for in_edge in dg.in_edges(edge[1])]
                        # if for considered child all parents are already initialized
                        # then add this node for initialization
                        if set(in_nodes_to_edge).issubset(set(sum(sequence_of_nodes, []))):
                            next_nodes.append(edge[1])
                sequence_of_nodes.append(next_nodes)

            # make a list of ints from list of lists
            sequence_of_nodes = sum(sequence_of_nodes, [])

            # now all nodes in sequence
            # can be initialized consequently
            for node_str_id in sequence_of_nodes:
                if node_str_id in sources:
                    # if considered node is source,
                    # give embedded texts as input
                    edges_outputs[node_str_id] = self.get_node_output(model_layers, node_str_id, dg, params, inp=inp)
                elif node_str_id in isolates:
                    # unreal condition
                    # if considered node is isolate,
                    # nothing to do
                    pass
                else:
                    # if considered node is not source and isolate,
                    # give all previous outputs as input
                    edges_outputs[node_str_id] = self.get_node_output(model_layers, node_str_id, dg, params,
                                                                      edges_outputs=edges_outputs)

            if len(sinks) == 1:
                # if the only sink,
                # output is this sink's output
                output = edges_outputs[sinks[0]]
            else:
                # if several sinks exist,
                # outputs will be concatenated
                outputs = []
                # collect outputs
                for sink in sinks:
                    outputs.append(edges_outputs[sink])
                try:
                    output = Concatenate()(outputs)
                except ValueError:
                    # outputs are of 2d and 3d shapes
                    # make them all 2d and concatenate
                    for i in range(len(outputs)):
                        if len(K.int_shape(outputs[i])) == 3:
                            outputs[i] = GlobalMaxPooling1D()(outputs[i])
                    output = Concatenate(axis=1)(outputs)

            if len(output.shape) == 3:
                output = GlobalMaxPooling1D()(output)
            full_outputs.append(output)

        summ = Add()(full_outputs)
        mult = Multiply()(full_outputs)

        try:
            subt = Subtract()(full_outputs)
            full_outputs.append(subt)
        except ValueError:
            pass
        full_outputs.append(summ)
        full_outputs.append(mult)
        
        output = Concatenate()(full_outputs)
        output = Dense(self.n_classes, activation=None)(output)
        activation = params.get("last_layer_activation", "sigmoid")
        act_output = Activation(activation)(output)
        model = Model(inputs=inputs, outputs=act_output)
        return model

    def save(self, fname=None):
        """
        Save the model parameters into <<fname>>_opt.json (or <<ser_file>>_opt.json)
        and model weights into <<fname>>.h5 (or <<ser_file>>.h5)
        Args:
            fname: file_path to save model. If not explicitly given seld.opt["ser_file"] will be used

        Returns:
            None
        """
        if type(self.opt["binary_mask"]) is list:
            pass
        else:
            self.opt["binary_mask"] = self.opt["binary_mask"].tolist()

        super().save(fname)
        return True