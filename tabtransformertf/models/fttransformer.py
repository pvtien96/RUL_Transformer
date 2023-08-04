import numpy as np
import tensorflow as tf
from tabtransformertf.models.tabtransformer import TransformerBlock
from tensorflow.keras.layers import (
    Dense,
    Flatten,
)
from tensorflow.keras import backend as K
from tensorflow.keras import layers
import math as m
from tabtransformertf.models.embeddings import CEmbedding, NEmbedding


class FTTransformerEncoder(tf.keras.Model):
    def __init__(
        self,
        categorical_features: list,
        numerical_features: list,
        numerical_data: np.array,
        categorical_data: np.array,
        y: np.array = None,
        task: str = None,
        embedding_dim: int = 32,
        depth: int = 4,
        heads: int = 8,
        attn_dropout: float = 0.1,
        ff_dropout: float = 0.1,
        numerical_embedding_type: str = "linear",
        numerical_bins: int = None,
        ple_tree_params: dict = {},
        explainable=False,
    ):
        """FTTransformer Encoder
        Args:
            categorical_features (list): names of categorical features
            numerical_features (list): names of numeric features
            categorical_lookup (dict): dictionary with categorical feature names as keys and adapted StringLookup layers as values
            out_dim (int): model output dimensions
            out_activation (str): model output activation
            embedding_dim (int, optional): embedding dimensions. Defaults to 32.
            depth (int, optional): number of transformer blocks. Defaults to 4.
            heads (int, optional): number of attention heads. Defaults to 8.
            attn_dropout (float, optional): dropout rate in transformer. Defaults to 0.1.
            ff_dropout (float, optional): dropout rate in mlps. Defaults to 0.1.
            mlp_hidden_factors (list[int], optional): numbers by which we divide dimensionality. Defaults to [2, 4].
            numerical_embeddings (dict, optional): dictionary with numerical feature names as keys and adapted numerical embedding layers as values. Defaults to None.
            numerical_embedding_type (str, optional): name of the numerical embedding procedure. Defaults to linear.
            use_column_embedding (bool, optional): flag to use fixed column positional embeddings. Defaults to True.
            explainable (bool, optional): flag to output importances inferred from attention weights. Defaults to False.
        """

        super(FTTransformerEncoder, self).__init__()
        self.numerical = numerical_features
        self.categorical = categorical_features
        self.numerical_embedding_type = numerical_embedding_type
        self.embedding_dim = embedding_dim
        self.explainable = explainable
        self.depth = depth
        self.heads = heads

        # Two main embedding modules
        if len(self.numerical) > 0:
            self.numerical_embeddings = NEmbedding(
                feature_names=self.numerical,
                X=numerical_data,
                y=y,
                task=task,
                emb_dim=embedding_dim,
                emb_type=numerical_embedding_type,
                n_bins=numerical_bins,
                tree_params=ple_tree_params,
            )
        if len(self.categorical) > 0:
            self.categorical_embeddings = CEmbedding(
                feature_names=self.categorical,
                X=categorical_data,
                emb_dim=embedding_dim,
            )

        # Transformers
        self.transformers = []
        for _ in range(depth):
            self.transformers.append(
                TransformerBlock(
                    embedding_dim,
                    heads,
                    embedding_dim,
                    att_dropout=attn_dropout,
                    ff_dropout=ff_dropout,
                    explainable=self.explainable,
                    post_norm=False,  # FT-Transformer uses pre-norm
                )
            )
        self.flatten_transformer_output = Flatten()

        # CLS token
        w_init = tf.random_normal_initializer()
        self.cls_weights = tf.Variable(
            initial_value=w_init(shape=(1, embedding_dim), dtype="float32"),
            trainable=True,
        )

    def call(self, inputs):
        # Start with CLS token
        cls_tokens = tf.repeat(
            self.cls_weights, repeats=tf.shape(inputs[self.numerical[0]])[0], axis=0
        )
        cls_tokens = tf.expand_dims(cls_tokens, axis=1)
        transformer_inputs = [cls_tokens]

        # If categorical features, add to list
        if len(self.categorical) > 0:
            cat_input = []
            for c in self.categorical:
                cat_input.append(inputs[c])

            cat_input = tf.stack(cat_input, axis=1)[:, :, 0]
            cat_embs = self.categorical_embeddings(cat_input)
            transformer_inputs += [cat_embs]

        # If numerical features, add to list
        if len(self.numerical) > 0:
            num_input = []
            for n in self.numerical:
                num_input.append(inputs[n])
            num_input = tf.stack(num_input, axis=1)[:, :, 0]
            num_embs = self.numerical_embeddings(num_input)
            transformer_inputs += [num_embs]

        # Prepare for Transformer
        transformer_inputs = tf.concat(transformer_inputs, axis=1)
        importances = []

        # Pass through Transformer blocks
        for transformer in self.transformers:
            if self.explainable:
                transformer_inputs, att_weights = transformer(transformer_inputs)
                importances.append(tf.reduce_sum(att_weights[:, :, 0, :], axis=1))
            else:
                transformer_inputs = transformer(transformer_inputs)

        if self.explainable:
            # Sum across the layers
            importances = tf.reduce_sum(tf.stack(importances), axis=0) / (
                self.depth * self.heads
            )
            return transformer_inputs, importances
        else:
            return transformer_inputs


class Time2Vec(tf.keras.Model):
    def __init__(
        self,
        time_features: list,
        kernel_size: int,
        t2v_emb_dim: int,
        periodic_activation="sin",
    ):
        super(Time2Vec, self).__init__(
            trainable=True, name="Time2VecLayer_" + periodic_activation.upper()
        )

        self.k = kernel_size
        self.features = time_features
        self.p_activation = periodic_activation

    def build(self, input_shape):
        self.wb = self.add_weight(
            name="wb", shape=(1, 1), initializer="uniform", trainable=True
        )

        self.bb = self.add_weight(
            name="bb", shape=(1, 1), initializer="uniform", trainable=True
        )

        self.wa = self.add_weight(
            name="wa", shape=(1, self.k), initializer="uniform", trainable=True
        )

        self.ba = self.add_weight(
            name="ba", shape=(1, self.k), initializer="uniform", trainable=True
        )

        super(Time2Vec, self).build(input_shape)

    def call(self, inputs, **kwargs):
        """
        : param inputs:  A Tensor with shape (batch_size, feature_size, 1)
        : param kwargs:
        : return: A Tensor with shape (batch_size, feature_size, length of time vector representation + 1)

        """
        # If time features, add to list
        if len(self.features) > 0:
            time_input = []
            for n in self.features:
                time_input.append(inputs[n])
            time_input = tf.stack(time_input, axis=1)[:, :, 0]

        time_input = tf.reshape(time_input, [-1, 16, 1])
        bias = self.wb * time_input + self.bb
        if self.p_activation.startswith("sin"):
            wgts = K.sin(K.dot(time_input, self.wa) + self.ba)
        elif self.p_activation.startswith("cos"):
            wgts = K.cos(K.dot(time_input, self.wa) + self.ba)
        else:
            raise NotImplementedError(
                "Neither sine or cosine periodic activation be selected."
            )

        return K.concatenate([bias, wgts], -1)

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], self.k + 1)


class FTTransformer(tf.keras.Model):
    def __init__(
        self,
        out_dim: int,
        out_activation: str,
        categorical_features: list = None,
        numerical_features: list = None,
        categorical_lookup: dict = None,
        embedding_dim: int = 32,
        depth: int = 4,
        heads: int = 8,
        attn_dropout: float = 0.1,
        ff_dropout: float = 0.1,
        numerical_embedding_type: str = None,
        numerical_embeddings: dict = None,
        explainable=False,
        encoder=None,
        t2v_encoder=None,
    ):
        super(FTTransformer, self).__init__()

        # Initialise encoder
        if encoder:
            self.encoder = encoder
        else:
            self.encoder = FTTransformerEncoder(
                categorical_features=categorical_features,
                numerical_features=numerical_features,
                categorical_lookup=categorical_lookup,
                embedding_dim=embedding_dim,
                depth=depth,
                heads=heads,
                attn_dropout=attn_dropout,
                ff_dropout=ff_dropout,
                numerical_embedding_type=numerical_embedding_type,
                numerical_embeddings=numerical_embeddings,
                explainable=explainable,
            )

        # mlp layers
        self.ln = tf.keras.layers.LayerNormalization()
        self.final_ff = Dense(embedding_dim // 2, activation="relu")
        self.output_layer = Dense(out_dim, activation=out_activation)

        # time2vec layer
        # self.time2vec_input = layers.Input(shape=(t2v_input_dim, 1))
        if t2v_encoder:
            self.t2v_encoder = t2v_encoder
        self.fl = layers.Flatten()

    def call(self, inputs):
        if self.encoder.explainable:
            x, expl = self.encoder(inputs)
        else:
            x = self.encoder(inputs)

        layer_norm_cls = self.ln(x[:, 0, :])
        layer_norm_cls = self.final_ff(layer_norm_cls)

        t2v_output = self.t2v_encoder(inputs)
        concate_layer = layers.concatenate([layer_norm_cls, self.fl(t2v_output)])

        output = self.output_layer(concate_layer)

        if self.encoder.explainable:
            # Explaianble models return two outputs
            return {"output": output, "importances": expl}
        else:
            return output
