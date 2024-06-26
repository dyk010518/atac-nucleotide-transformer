import logging
import warnings
from functools import partial
from typing import Dict, Iterable, List, Optional, Sequence, Union, Literal

import numpy as np
import pandas as pd
import torch
from anndata import AnnData
import scipy

from scvi import REGISTRY_KEYS
from scvi.data import AnnDataManager
from scvi.data.fields import (
    CategoricalJointObsField,
    CategoricalObsField,
    LayerField,
    NumericalJointObsField,
    NumericalObsField,
)
from scvi.model._utils import (
    _get_batch_code_from_category,
    _init_library_size,
    scrna_raw_counts_properties,
)
from scvi.model.base import UnsupervisedTrainingMixin
from scvi.utils import setup_anndata_dsp

from scvi.model.base import ArchesMixin, BaseModelClass, RNASeqMixin, VAEMixin
from scvi.model.base._utils import _de_core

from poisson_atac.module import BinaryVAE

logger = logging.getLogger(__name__)

_SCVI_LATENT_QZM = "_scvi_latent_qzm"
_SCVI_LATENT_QZV = "_scvi_latent_qzv"
_SCVI_OBSERVED_LIB_SIZE = "_scvi_observed_lib_size"


class BinaryVI(ArchesMixin, RNASeqMixin, VAEMixin, UnsupervisedTrainingMixin, BaseModelClass):
    """
    Variational inference on peaks using binarized data and observed sequence coverage.
    Parameters
    ----------
    adata
        AnnData object that has been registered via :meth:`~poisson_atac.model.BinaryVI.setup_anndata`.
    n_hidden
        Number of nodes per hidden layer. If `None`, defaults to square root
        of number of regions.
    n_latent
        Dimensionality of the latent space. If `None`, defaults to square root
        of `n_hidden`.
    n_layers_encoder
        Number of hidden layers used for encoder NN.
    n_layers_decoder
        Number of hidden layers used for decoder NN.
    dropout_rate
        Dropout rate for neural networks
    latent_distribution
        One of
        * ``'normal'`` - Normal distribution (Default)
        * ``'ln'`` - Logistic normal distribution (Normal(0, I) transformed by softmax)
    deeply_inject_covariates
        Whether to deeply inject covariates into all layers of the decoder. If False (default),
        covariates will only be included in the input layer.
    **model_kwargs
        Keyword args for :class:`~poisson_atac.module.BinaryVAE`
    """

    def __init__(
        self,
        adata: AnnData,
        n_hidden: Optional[int] = None,
        n_latent: Optional[int] = None,
        n_layers_encoder: int = 2,
        n_layers_decoder: int = 2,
        dropout_rate: float = 0.1,
        use_batch_norm: Literal["encoder", "decoder", "none", "both"] = "none",
        use_layer_norm: Literal["encoder", "decoder", "none", "both"] = "both",
        latent_distribution: Literal["normal", "ln"] = "normal",
        deeply_inject_covariates: bool = False,
        encode_covariates: bool = False,
        **model_kwargs,
    ):
        super().__init__(adata)

        n_cats_per_cov = (
            self.adata_manager.get_state_registry(
                REGISTRY_KEYS.CAT_COVS_KEY
            ).n_cats_per_key
            if REGISTRY_KEYS.CAT_COVS_KEY in self.adata_manager.data_registry
            else []
        )
        n_batch = self.summary_stats.n_batch
        
        use_size_factor_key = (
            REGISTRY_KEYS.SIZE_FACTOR_KEY in self.adata_manager.data_registry
        )
        
        library_log_means, library_log_vars = None, None
        if not use_size_factor_key:
            library_log_means, library_log_vars = _init_library_size(
                self.adata_manager, n_batch
            )
            
        self.module = BinaryVAE(
            n_input_regions=self.summary_stats.n_vars,
            n_batch=n_batch,
            n_hidden=n_hidden,
            n_latent=n_latent,
            n_layers_encoder=n_layers_encoder,
            n_layers_decoder=n_layers_decoder,
            n_continuous_cov=self.summary_stats.get("n_extra_continuous_covs", 0),
            n_cats_per_cov=n_cats_per_cov,
            dropout_rate=dropout_rate,
            use_batch_norm=use_batch_norm,
            use_layer_norm=use_layer_norm,
            latent_distribution=latent_distribution,
            deeply_inject_covariates=deeply_inject_covariates,
            encode_covariates=encode_covariates,            
            use_size_factor_key=use_size_factor_key,
            library_log_means=library_log_means,
            library_log_vars=library_log_vars,
            **model_kwargs,
        )
        
        self._model_summary_string = (
            "BinaryVI Model with params: \nn_hidden: {}, n_latent: {}, n_layers_encoder: {}, "
            "n_layers_decoder: {} , dropout_rate: {}, latent_distribution: {}, deep injection: {}, "
            "encode_covariates: {}"
        ).format(
            self.module.n_hidden,
            self.module.n_latent,
            n_layers_encoder,
            n_layers_decoder,
            dropout_rate,
            latent_distribution,
            deeply_inject_covariates,
            encode_covariates,
        )
        self.n_latent = n_latent
        self.init_params_ = self._get_init_params(locals())

    def train(
        self,
        max_epochs: int = 500,
        lr: float = 1e-4,
        use_gpu: Optional[Union[str, int, bool]] = None,
        train_size: float = 0.9,
        validation_size: Optional[float] = None,
        batch_size: int = 128,
        weight_decay: float = 1e-3,
        eps: float = 1e-08,
        early_stopping: bool = True,
        check_val_every_n_epoch: Optional[int] = None,
        n_steps_kl_warmup: Union[int, None] = None,
        n_epochs_kl_warmup: Union[int, None] = 50,
        plan_kwargs: Optional[dict] = None,
        **kwargs,
    ):
        """
        Trains the model using amortized variational inference.
        Parameters
        ----------
        max_epochs
            Number of passes through the dataset.
        lr
            Learning rate for optimization.
        use_gpu
            Use default GPU if available (if None or True), or index of GPU to use (if int),
            or name of GPU (if str, e.g., `'cuda:0'`), or use CPU (if False).
        train_size
            Size of training set in the range [0.0, 1.0].
        validation_size
            Size of the test set. If `None`, defaults to 1 - `train_size`. If
            `train_size + validation_size < 1`, the remaining cells belong to a test set.
        batch_size
            Minibatch size to use during training.
        weight_decay
            weight decay regularization term for optimization
        eps
            Optimizer eps
        early_stopping
            Whether to perform early stopping with respect to the validation set.
        early_stopping_patience
            How many epochs to wait for improvement before early stopping
        save_best
            Save the best model state with respect to the validation loss (default), or use the final
            state in the training procedure
        check_val_every_n_epoch
            Check val every n train epochs. By default, val is not checked, unless `early_stopping` is `True`.
            If so, val is checked every epoch.
        n_steps_kl_warmup
            Number of training steps (minibatches) to scale weight on KL divergences from 0 to 1.
            Only activated when `n_epochs_kl_warmup` is set to None. If `None`, defaults
            to `floor(0.75 * adata.n_obs)`.
        n_epochs_kl_warmup
            Number of epochs to scale weight on KL divergences from 0 to 1.
            Overrides `n_steps_kl_warmup` when both are not `None`.
        plan_kwargs
            Keyword args for :class:`~scvi.train.TrainingPlan`. Keyword arguments passed to
            `train()` will overwrite values present in `plan_kwargs`, when appropriate.
        **kwargs
            Other keyword args for :class:`~scvi.train.Trainer`.
        """
        update_dict = dict(
            lr=lr,
            weight_decay=weight_decay,
            eps=eps,
            n_epochs_kl_warmup=n_epochs_kl_warmup,
            n_steps_kl_warmup=n_steps_kl_warmup,
            optimizer="AdamW",
        )
        if plan_kwargs is not None:
            plan_kwargs.update(update_dict)
        else:
            plan_kwargs = update_dict
        

        super().train(
            max_epochs=max_epochs,
            train_size=train_size,
            use_gpu=use_gpu,
            validation_size=validation_size,
            early_stopping=early_stopping,
            early_stopping_monitor="reconstruction_loss_validation",
            plan_kwargs=plan_kwargs,
            check_val_every_n_epoch=check_val_every_n_epoch,
            batch_size=batch_size,
            **kwargs,
        )

    @torch.no_grad()
    def get_latent_library_size(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        give_mean: bool = True,
        batch_size: Optional[int] = None,
    ) -> np.ndarray:
        r"""
        Returns the latent library size for each cell.
        This is denoted as :math:`\ell_n` in the scVI paper.
        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        give_mean
            Return the mean or a sample from the posterior distribution.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        """
        self._check_if_trained(warn=False)

        adata = self._validate_anndata(adata)
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size
        )
        libraries = []
        for tensors in scdl:
            inference_inputs = self.module._get_inference_input(tensors)
            outputs = self.module.inference(**inference_inputs)

            library = outputs["library"]
            if not give_mean:
                library = torch.exp(library)
            else:
                ql_m = outputs["ql_m"]
                ql_v = outputs["ql_v"]
                if ql_m is None or ql_v is None:
                    raise RuntimeError(
                        "The module for this model does not compute the posterior distribution "
                        "for the library size. Set `give_mean` to False to use the observed library size instead."
                    )
                library = torch.distributions.LogNormal(ql_m, ql_v.sqrt()).mean
            libraries += [library.cpu()]
        return torch.cat(libraries).numpy()

    @torch.no_grad()
    def get_region_factors(self):
        """Return region-specific factors."""
        raise NotImplementedError("This function is not yet implemented")

    @torch.no_grad()
    # This is adapted from scvi
    def get_accessibility_estimates(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        transform_batch: Optional[Sequence[Union[int, str]]] = None,
        region_list: Optional[Sequence[str]] = None,
        library_size: Union[float, Literal["latent"]] = 1,
        n_samples: int = 1,
        n_samples_overall: int = None,
        batch_size: Optional[int] = None,
        return_mean: bool = True,
        return_numpy: Optional[bool] = None,
        logit=False,
    ) -> Union[np.ndarray, pd.DataFrame]:
        """
        Returns the decoded accessibility.
        Parameters
        ----------
        adata
            AnnData object with equivalent structure to initial AnnData. If `None`, defaults to the
            AnnData object used to initialize the model.
        indices
            Indices of cells in adata to use. If `None`, all cells are used.
        transform_batch
            Batch to condition on.
            If transform_batch is:
            - None, then real observed batch is used.
            - int, then batch transform_batch is used.
        region_list
            Return probability of accessibiilty for a subset of regions.
            This can save memory when working with large datasets and few regions are
            of interest.
        library_size
            Scale the fragment frequencies to a common library size.
            This allows accessibility levels to be interpreted on a common scale of relevant
            magnitude. If set to `"latent"`, use the latent libary size.
        n_samples
            Number of posterior samples to use for estimation.
        batch_size
            Minibatch size for data loading into model. Defaults to `scvi.settings.batch_size`.
        return_mean
            Whether to return the mean of the samples.
        return_numpy
            Return a :class:`~numpy.ndarray` instead of a :class:`~pandas.DataFrame`. DataFrame includes
            gene names as columns. If either `n_samples=1` or `return_mean=True`, defaults to `False`.
            Otherwise, it defaults to `True`.
        Returns
        -------
        If `n_samples` > 1 and `return_mean` is False, then the shape is `(samples, cells, genes)`.
        Otherwise, shape is `(cells, genes)`. In this case, return type is :class:`~pandas.DataFrame` unless `return_numpy` is True.
        """
        adata = self._validate_anndata(adata)
        if indices is None:
            indices = np.arange(adata.n_obs)
        if n_samples_overall is not None:
            indices = np.random.choice(indices, n_samples_overall)
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size
        )

        transform_batch = _get_batch_code_from_category(
            self.get_anndata_manager(adata, required=True), transform_batch
        )

        if region_list is None:
            region_mask = slice(None)
        else:
            all_regions = adata.var_names
            region_mask = [True if region in region_list else False for region in all_regions]

        if n_samples > 1 and return_mean is False:
            if return_numpy is False:
                warnings.warn(
                    "return_numpy must be True if n_samples > 1 and return_mean is False, returning np.ndarray"
                )
            return_numpy = True
        if library_size == "latent":
            generative_output_key = "px_rate"
            shift = 0
        else:
            generative_output_key = "px_scale"
            if library_size > 0:
                shift = scipy.special.logit(library_size/self.summary_stats.n_vars)
            else: 
                shift = 0

        accs = []
        for tensors in scdl:
            per_batch_accs = []
            for batch in transform_batch:
                generative_kwargs = self._get_transform_batch_gen_kwargs(batch)
                inference_kwargs = dict(n_samples=n_samples)
                _, generative_outputs = self.module.forward(
                    tensors=tensors,
                    inference_kwargs=inference_kwargs,
                    generative_kwargs=generative_kwargs,
                    compute_loss=False,
                )
                output = generative_outputs[generative_output_key]
                output = output[..., region_mask]
                if library_size!="latent":
                    output += shift
                    if not logit:
                        output = torch.sigmoid(output)
                output = output.cpu().numpy()
                per_batch_accs.append(output)
            per_batch_accs = np.stack(
                per_batch_accs
            )  # shape is (len(transform_batch) x batch_size x n_var)
            accs += [per_batch_accs.mean(0)]

        if n_samples > 1:
            # The -2 axis correspond to cells.
            accs = np.concatenate(accs, axis=-2)
        else:
            accs = np.concatenate(accs, axis=0)
        if n_samples > 1 and return_mean:
            accs = accs.mean(0)
            
        if return_numpy is None or return_numpy is False:
            return pd.DataFrame(
                accs,
                columns=adata.var_names[region_mask],
                index=adata.obs_names[indices],
            )
        else:
            return accs

    def differential_accessibility(
        self,
        adata: Optional[AnnData] = None,
        groupby: Optional[str] = None,
        group1: Optional[Iterable[str]] = None,
        group2: Optional[str] = None,
        idx1: Optional[Union[Sequence[int], Sequence[bool], str]] = None,
        idx2: Optional[Union[Sequence[int], Sequence[bool], str]] = None,
        mode: Literal["vanilla", "change"] = "change",
        delta: float = 0.05,
        batch_size: Optional[int] = None,
        all_stats: bool = True,
        batch_correction: bool = False,
        batchid1: Optional[Iterable[str]] = None,
        batchid2: Optional[Iterable[str]] = None,
        fdr_target: float = 0.05,
        silent: bool = False,
        two_sided: bool = True,
        library_size = 0,
        **kwargs,
    ) -> pd.DataFrame:
        r"""\
        A unified method for differential accessibility analysis.
        Implements `"vanilla"` DE :cite:p:`Lopez18`. and `"change"` mode DE :cite:p:`Boyeau19`.
        Parameters
        ----------
        {doc_differential_expression}
        two_sided
            Whether to perform a two-sided test, or a one-sided test.
        **kwargs
            Keyword args for :meth:`scvi.model.base.DifferentialComputation.get_bayes_factors`
        Returns
        -------
        Differential accessibility DataFrame with the following columns:
        prob_da
            the probability of the region being differentially accessible
        is_da_fdr
            whether the region passes a multiple hypothesis correction procedure with the target_fdr
            threshold
        bayes_factor
            Bayes Factor indicating the level of significance of the analysis
        effect_size
            the effect size, computed as (accessibility in population 2) - (accessibility in population 1)
        emp_effect
            the empirical effect, based on observed detection rates instead of the estimated accessibility
            scores from the PeakVI model
        est_prob1
            the estimated probability of accessibility in population 1
        est_prob2
            the estimated probability of accessibility in population 2
        emp_prob1
            the empirical (observed) probability of accessibility in population 1
        emp_prob2
            the empirical (observed) probability of accessibility in population 2
        """
        adata = self._validate_anndata(adata)
        col_names = adata.var_names
        
        #use mean library size
        model_fn = partial(
            self.get_accessibility_estimates, return_mean=False, batch_size=batch_size, library_size=library_size, logit=True
        )

        # TODO check if change_fn in kwargs and raise error if so
        def change_fn(a, b):
            return a - b
        
        if two_sided:
            def m1_domain_fn(samples):
                return np.abs(samples) >= delta

        else:
            def m1_domain_fn(samples):
                return samples >= delta

        result = _de_core(
            adata_manager=self.get_anndata_manager(adata, required=True),
            model_fn=model_fn,
            groupby=groupby,
            group1=group1,
            group2=group2,
            idx1=idx1,
            idx2=idx2,
            all_stats=all_stats,
            all_stats_fn=scatac_raw_counts_properties,
            col_names=col_names,
            mode=mode,
            batchid1=batchid1,
            batchid2=batchid2,
            delta=delta,
            batch_correction=batch_correction,
            fdr=fdr_target,
            change_fn=change_fn,
            m1_domain_fn=m1_domain_fn,
            silent=silent,
            **kwargs,
        )

        return result
    
    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        layer: Optional[str] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        size_factor_key: Optional[str] = None,
        categorical_covariate_keys: Optional[List[str]] = None,
        continuous_covariate_keys: Optional[List[str]] = None,
        **kwargs,
    ):
        """
        %(summary)s.
        Parameters
        ----------
        %(param_layer)s
        %(param_batch_key)s
        %(param_labels_key)s
        %(param_size_factor_key)s
        %(param_cat_cov_keys)s
        %(param_cont_cov_keys)s
        """
        
        setup_method_args = cls._get_setup_method_args(**locals())
        anndata_fields = [
            LayerField(REGISTRY_KEYS.X_KEY, layer, is_count_data=True),
            CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
            CategoricalObsField(REGISTRY_KEYS.LABELS_KEY, labels_key),
            NumericalObsField(
                REGISTRY_KEYS.SIZE_FACTOR_KEY, size_factor_key, required=False
            ),
            CategoricalJointObsField(
                REGISTRY_KEYS.CAT_COVS_KEY, categorical_covariate_keys
            ),
            NumericalJointObsField(
                REGISTRY_KEYS.CONT_COVS_KEY, continuous_covariate_keys
            ),
        ]
        adata_manager = AnnDataManager(
            fields=anndata_fields, setup_method_args=setup_method_args
        )
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)
        
        