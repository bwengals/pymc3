import numpy as np
from scipy import stats
from tqdm import tqdm

import theano.tensor as tt
from theano.tensor.slinalg import Solve

import pymc3 as pm
from .mean import Zero, Mean
from .cov import Covariance
from ..distributions import (Normal, MvNormal, Continuous,
                             draw_values, generate_samples)
from ..model import modelcontext, Deterministic, ObservedRV
from ..distributions.dist_math import Cholesky

__all__ = ['GP', 'sample_gp']

CHOL_CONST = True
cholesky = Cholesky(nofail=True, lower=True)
solve_lower = Solve(A_structure="lower_triangular")
solve_upper = Solve(A_structure="upper_triangular")

def stabilize(K):
    n = K.shape[0]
    return K + 1e-6 * (tt.nlinalg.trace(K)/n) * tt.eye(n)

class GPValueError(Exception):
    """Raise for exceptions involving invalid arguments to GP constructor"""


def GP(name, X, mean_func=None, cov_func=None,
       cov_func_noise=None, sigma=None,
       approx=None, n_inducing=None, inducing_points=None,
       observed=None, model=None, *args, **kwargs):
    """Gausian process constructor
    Parameters
    ----------
    X : array
        Grid of points to evaluate Gaussian process over.
    mean_func : Mean
        Mean function of Gaussian process
    cov_func : Covariance
        Covariance function of Gaussian process
    cov_func_noise : Covariance
        Covariance function of noise process (ignored for approximations)
    sigma : scalar or array
        Noise standard deviation
    approx : None or string,
        Allowed values are 'FITC' and 'VFE'
    n_inducing : integer
        The number of inducing points to use
    inducing_points : array
        The inducing points to use
    observed : None or array
        The observed 'y' values, use if likelihood is Gaussian
    model : Model
        Optional if in `with` context manager.
    """

    model = modelcontext(model)

    if mean_func is None:
        mean_func = Zero()
    else:
        if not isinstance(mean_func, Mean):
            raise GPValueError('mean_func must be a subclass of Mean')
    if cov_func is None:
        raise GPValueError('A covariance function must be specified for GP')
    if not isinstance(cov_func, Covariance):
        raise GPValueError('cov_func must be a subclass of Covariance')

    # NONCONJUGATE
    if observed is None:
        gp = GPFullNonConjugate(name, X, mean_func, cov_func)
        return gp.RV

    # CONJUGATE
    if all(value is None for value in [approx, n_inducing, inducing_points]):
        if sigma is None and cov_func_noise is None:
            raise GPValueError(('Must provide a value or a prior '
                              'for the noise variance'))
        if sigma is not None and cov_func_noise is None:
            cov_func_noise = lambda X: tt.square(sigma) * tt.eye(X.shape[0])
        return GPFullConjugate(name, X, mean_func, cov_func, cov_func_noise,
                               observed=observed)
    else:
        approx = approx.upper()

    # CONJUGATE, APPROXIMATION
    if inducing_points is None and n_inducing is not None:
        # initialize inducing points with K-means
        from scipy.cluster.vq import kmeans
        # first whiten X
        if not isinstance(X, np.ndarray):
            X = X.value
        scaling = np.std(X, 0)
        Xw = X / scaling
        Xu, distortion = kmeans(Xw, n_inducing)
        inducing_points = Xu * scaling

    if approx is None:
        pm._log.info("Using VFE approximation")
        approx = "VFE"

    if approx not in ["VFE", "FITC"]:
        raise GPValueError(("'FITC' or 'VFE' are the implemented "
                          "GP approximations"))

    if inducing_points is None and n_inducing is None:
        raise GPValueError(("Must specify one of 'inducing_points' "
                          "or 'n_inducing'"))

    inducing_points = tt.as_tensor_variable(inducing_points)
    return GPSparseConjugate(name, X, mean_func, cov_func, sigma,
                             approx, inducing_points, observed=observed)


class GPBase(object):
    def random(self, point=None, size=None, X_values=None, obs_noise=False,
               y=None, from_prior=False, **kwargs):
        if from_prior:
            # draw from prior
            mean, cov = self.prior(obs_noise)
        else:
            # draw from conditional
            mean, cov = self.conditional(X_values, y, obs_noise)
        mu, cov = draw_values([mean, cov], point=point)

        def _random(mean, cov, size=None):
            return stats.multivariate_normal.rvs(
                mean, cov, None if size == mean.shape else size)

        samples = generate_samples(_random,
                                   mean=mu, cov=cov,
                                   dist_shape=mu.shape,
                                   broadcast_shape=mu.shape,
                                   size=size)
        return samples

    def prior(self, obs_noise=False):
        raise NotImplementedError

    def conditional(self, Xs, y, obs_noise=False):
        raise NotImplementedError

    def logp(self, y):
        return 0.0

    def _repr_latex_(self, name=None):
        return (r"${} \sim \mathcal{{GP}}".format(name) +
                r"(\mathit{{\mu}}(x), \mathit{{K}}(x, x'))$")


class GPFullNonConjugate(GPBase):
    """Gausian process

    Parameters
    ----------
    X : array
        Array of predictor variables.
    mean_func : Mean
        Mean function of Gaussian process
    cov_func : Covariance
        Covariance function of Gaussian process
    """
    def __init__(self, name, X, mean_func, cov_func):
        self.name = name
        self.X = X
        self.nf = X.shape[0]
        self.K = cov_func
        self.m = mean_func
        self.mean = self.mode = self.m(X)

    def prior(self):
        mean = self.m(self.X)
        cov = self.K(self.X)
        return mean, stabilize(cov)

    def conditional(self, Xs, *args, **kwargs):
        v = kwargs.pop("v", self.v)
        Xs = tt.as_tensor_variable(Xs)
        Kxx = self.K(self.X)
        Kxs = self.K(self.X, Xs)
        Kss = self.K(Xs)

        L = cholesky(stabilize(Kxx))
        A = solve_lower(L, Kxs)

        cov = Kss - tt.dot(tt.transpose(A), A)
        mean = self.m(Xs) + tt.dot(tt.transpose(A), v)
        return mean, stabilize(cov)

    @property
    def RV(self):
        self.v = Normal(self.name + "_rotated_", mu=0.0, sd=1.0,
                        shape=self.nf, testval=np.zeros(self.nf))
        L = cholesky(stabilize(self.K(self.X)))
        f = Deterministic(self.name, tt.dot(L, self.v))
        f.distribution = self
        return f


class GPFullConjugate(GPBase, Continuous):
    """Gausian process

    Parameters
    ----------
    X : array
        Array of predictor variables.
    mean_func : Mean
        Mean function of Gaussian process
    cov_func : Covariance
        Covariance function of Gaussian process
    cov_func_noise : Covariance
        Covariance function of noise Gaussian process
    """
    def __init__(self, X, mean_func, cov_func, cov_func_noise,
                 *args, **kwargs):
        self.X = X
        self.nf = self.X.shape[0]
        self.K = cov_func
        self.Kn = cov_func_noise
        self.m = mean_func
        self.mean = self.mode = self.m(X)

        kwargs.setdefault("shape", X.squeeze().shape)
        super(GPFullConjugate, self).__init__(*args, **kwargs)

    def prior(self, obs_noise=False):
        mean = self.m(self.X)
        if obs_noise:
            cov = self.K(self.X) + self.Kn(self.X)
        else:
            cov = self.K(self.X)
        return mean, stabilize(cov)

    def conditional(self, Xs, y, obs_noise=False):
        Kxx = self.K(self.X)
        Knx = self.Kn(self.X)
        Kxs = self.K(self.X, Xs)
        Kss = self.K(Xs)

        r = y - self.m(self.X)
        L = cholesky(stabilize(Kxx) + Knx)
        A = solve_lower(L, Kxs)
        V = solve_lower(L, r)
        mean = tt.dot(tt.transpose(A), V) + self.m(Xs)
        if obs_noise:
            cov = self.Kn(Xs) + Kss - tt.dot(tt.transpose(A), A)
        else:
            cov = Kss - tt.dot(tt.transpose(A), A)
        return mean, stabilize(cov)

    def logp(self, y):
        mean = self.m(self.X)
        L = cholesky(stabilize(self.K(self.X)) + self.Kn(self.X))
        return MvNormal.dist(mu=mean, chol=L).logp(y)


class GPSparseConjugate(GPBase, Continuous):
    """Sparse Gausian process approximation

    Parameters
    ----------
    X : array
        Array of predictor variables.
    mean_func : Mean
        Mean function of Gaussian process
    cov_func : Covariance
        Covariance function of Gaussian process
    sigma : scalar or array
        Noise standard deviation
    approx : string
        Allowed values are 'FITC' and 'VFE'
    inducing_points : array
        Grid of points to evaluate Gaussian process over.
    """
    def __init__(self, X, mean_func, cov_func, sigma, approx,
                 inducing_points, *args, **kwargs):
        self.X = X
        self.nf = self.X.shape[0]
        self.K = cov_func
        self.m = mean_func
        self.mean = self.mode = self.m(X)
        self.sigma2 = tt.square(sigma)

        self.approx = approx
        self.Xu = inducing_points
        self.nu = self.Xu.shape[0]

        kwargs.setdefault("shape", X.squeeze().shape)
        super(GPSparseConjugate, self).__init__(*args, **kwargs)

    def prior(self, obs_noise=False):
        Kuu = self.K(self.Xu)
        Kuf = self.K(self.Xu, self.X)
        Luu = cholesky(stabilize(Kuu))
        A = solve_lower(Luu, Kuf)
        Kffd = self.K(self.X, diag=True)
        Qff = tt.dot(tt.transpose(A), A)
        mean = self.m(self.X)
        # fitc vs vfe prior?
        if obs_noise:
            cov = Qff - (tt.diag(Qff) - Kffd) + self.sigma2 * tt.eye(self.nf)
        else:
            cov = Qff - (tt.diag(Qff) - Kffd)
        return mean, stabilize(cov)

    def conditional(self, Xs, y, obs_noise=False):
        Kuu = self.K(self.Xu)
        Kuf = self.K(self.Xu, self.X)
        Luu = cholesky(stabilize(Kuu))
        A = solve_lower(Luu, Kuf)
        Qffd = tt.sum(A * A, 0)
        if self.approx == "FITC":
            Kffd = self.K(self.X, diag=True)
            Lamd = tt.clip(Kffd - Qffd, 0.0, np.inf) + self.sigma2
        elif self.approx == "VFE":
            Lamd = tt.ones_like(Qffd) * self.sigma2
        else:
            raise NotImplementedError(self.approx)
        A_l = A / Lamd
        L_B = cholesky(tt.eye(self.nu) + tt.dot(A_l, tt.transpose(A)))
        r = y - self.m(self.X)
        r_l = r / Lamd
        c = solve_lower(L_B, tt.dot(A, r_l))
        Kus = self.K(self.Xu, Xs)
        As = solve_lower(Luu, Kus)
        mean = tt.dot(tt.transpose(As), solve_upper(tt.transpose(L_B), c))
        C = solve_lower(L_B, As)
        if obs_noise:
            cov = self.K(Xs, Xs) - tt.dot(tt.transpose(As), As)\
                                 + tt.dot(tt.transpose(C), C)\
                                 + self.sigma2*tt.eye(Xs.shape[0])
        else:
            cov = self.K(Xs, Xs) - tt.dot(tt.transpose(As), As)\
                                 + tt.dot(tt.transpose(C), C)
        return mean, stabilize(cov)

    def logp(self, y):
        Kuu = self.K(self.Xu, self.Xu)
        Kuf = self.K(self.Xu, self.X)
        Luu = cholesky(stabilize(Kuu))
        A = solve_lower(Luu, Kuf)
        Qffd = tt.sum(A * A, 0)
        if self.approx == "FITC":
            Kffd = self.K(self.X, diag=True)
            Lamd = tt.clip(Kffd - Qffd, 0.0, np.inf) + self.sigma2
            trace = 0.0
        elif self.approx == "VFE":
            Lamd = tt.ones_like(Qffd) * self.sigma2
            trace = ((1.0 / (2.0 * self.sigma2)) *
                     (tt.sum(self.K(self.X, diag=True)) -
                      tt.sum(tt.sum(A * A, 0))))
        else:
            raise NotImplementedError(self.approx)
        A_l = A / Lamd
        L_B = cholesky(tt.eye(self.nu) + tt.dot(A_l, tt.transpose(A)))
        r = y - self.m(self.X)
        r_l = r / Lamd
        c = solve_lower(L_B, tt.dot(A, r_l))
        constant = 0.5 * self.nf * tt.log(2.0 * np.pi)
        logdet = 0.5 * tt.sum(tt.log(Lamd)) + tt.sum(tt.log(tt.diag(L_B)))
        quadratic = 0.5 * (tt.dot(r, r_l) - tt.dot(c, c))
        return -1.0 * (constant + logdet + quadratic + trace)


def sample_gp(trace, gp, X_values, n_samples=None, obs_noise=True,
              from_prior=False, model=None, random_seed=None,
              progressbar=True):
    """Generate samples from a posterior Gaussian process.

    Parameters
    ----------
    trace : backend, list, or MultiTrace
        Trace generated from MCMC sampling.
    gp : Gaussian process object
        The GP variable to sample from.
    X_values : array
        Grid of new values at which to sample GP.  If `None`, returns
        samples from the prior.
    n_samples : int
        Number of posterior predictive samples to generate. Defaults to the
        length of `trace`
    obs_noise : bool
        Flag for including observation noise in sample.  Does not apply to GPs
        used with non-conjugate likelihoods.  Defaults to False.
    from_prior: bool
        Flag for draw from GP prior.  Defaults to False.
    model : Model
        Model used to generate `trace`. Optional if in `with` context manager.
    random_seed : integer > 0
        Random number seed for sampling.
    progressbar : bool
        Flag for showing progress bar.

    Returns
    -------
    Array of samples from posterior GP evaluated at X_values.
    """
    model = modelcontext(model)

    if random_seed:
        np.random.seed(random_seed)

    if n_samples is None:
        n_samples = len(trace)

    indices = tqdm(np.random.choice(np.arange(len(trace)),
                                    n_samples, replace=False),
                   total=n_samples, disable=not progressbar)

    # using observed=y to determine conjugacy? think about more
    if isinstance(gp, ObservedRV):
        y = [v for v in model.observed_RVs if v.name == gp.name][0]
    else:
        y = None

    try:
        samples = []
        for ix in indices:
            samples.append(gp.distribution.random(trace[ix], None,
                           X_values, obs_noise, y, from_prior))
    except KeyboardInterrupt:
        pass
    finally:
        if progressbar:
            indices.close()
    return np.array(samples)
