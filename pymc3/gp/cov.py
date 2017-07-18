import theano
import theano.tensor as tt
import numpy as np
from functools import reduce

__all__ = ['ExpQuad',
           'RatQuad',
           'Exponential',
           'Matern52',
           'Matern32',
           'Linear',
           'Polynomial',
           'Cosine',
           'WarpedInput',
           'Gibbs']


class Covariance(object):
    R"""
    Base class for all kernels/covariance functions.

    Parameters
    ----------
    input_dim : integer
        The number of input dimensions, or columns of X (or Xs)
        the kernel will operate on.
    active_dims : A list of booleans whose length equals input_dim.
        The booleans indicate whether or not the covariance function operates
        over that dimension or column of X.
    """

    def __init__(self, input_dim, active_dims=None):
        self.input_dim = input_dim
        if active_dims is None:
            self.active_dims = np.arange(input_dim)
        else:
            self.active_dims = np.array(active_dims)
            if len(active_dims) != input_dim:
                raise ValueError("Length of active_dims must match input_dim")

    def __call__(self, X, Xs=None, diag=False):
        R"""
        Evaluate the kernel/covariance function.

        Parameters
        ----------
        X : The training inputs to the kernel.
        Xs : The optional prediction set of inputs the kernel.
            If Xs is None, Xs = X.
        diag: bool
            Return only the diagonal of the covariance function.
            Default is False.
        """
        if diag:
            return self.diag(X)
        else:
            return self.full(X, Xs)

    def _slice(self, X, Xs):
        X = X[:, self.active_dims]
        if Xs is not None:
            Xs = Xs[:, self.active_dims]
        return X, Xs

    def __add__(self, other):
        return Add([self, other])

    def __mul__(self, other):
        return Prod([self, other])

    def __radd__(self, other):
        return self.__add__(other)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __array_wrap__(self, result):
        """
        Required to allow radd/rmul by numpy arrays.
        """
        r, c = result.shape
        A = np.zeros((r, c))
        for i in range(r):
            for j in range(c):
                A[i, j] = result[i, j].factor_list[1]
        if isinstance(result[0][0], Add):
            return result[0][0].factor_list[0] + A
        elif isinstance(result[0][0], Prod):
            return result[0][0].factor_list[0] * A
        else:
            raise RuntimeError


class Combination(Covariance):
    def __init__(self, factor_list):
        input_dim = np.max([factor.input_dim for factor in
                            filter(lambda x: isinstance(x, Covariance),
                                   factor_list)])
        super(Combination, self).__init__(input_dim=input_dim)
        self.factor_list = []
        for factor in factor_list:
            if isinstance(factor, self.__class__):
                self.factor_list.extend(factor.factor_list)
            else:
                self.factor_list.append(factor)

    def merge_factors(self, X, Xs=None, diag=False):
        # this function makes sure diag=True is handled properly
        factor_list = []
        for factor in self.factor_list:

            # if factor is a Covariance
            if isinstance(factor, Covariance):
                factor_list.append(factor(X, Xs, diag))
                continue

            # if factor is a numpy array
            if isinstance(factor, np.ndarray):
                if np.ndim(factor) == 2:
                    if diag:
                        factor_list.append(np.diag(factor))
                        continue

            # if factor is a theano variable with ndim attribute
            if isinstance(factor, (tt.TensorConstant,
                                     tt.TensorVariable,
                                     tt.sharedvar.TensorSharedVariable)):
                if factor.ndim == 2:
                    if diag:
                        factor_list.append(tt.diag(factor))
                        continue

            # othewise
            factor_list.append(factor)

        return factor_list


class Add(Combination):
    def __call__(self, X, Xs=None, diag=False):
        return reduce((lambda x, y: x + y), self.merge_factors(X, Xs, diag))


class Prod(Combination):
    def __call__(self, X, Xs=None, diag=False):
        return reduce((lambda x, y: x * y), self.merge_factors(X, Xs, diag))


class WhiteNoise(Covariance):
    R"""
    White noise covariance function.

    .. math::

       k(x, x') = \sigma^2 \mathrm{I}
    """

    def __init__(self, input_dim, sigma, active_dims=None):
        super(WhiteNoise, self).__init__(input_dim, active_dims)
        self.sigma = sigma

    def diag(self, X):
        return tt.square(self.sigma) * tt.ones(tt.stack([X.shape[0], ]))

    def full(self, X, Xs=None):
        if Xs is None:
            return tt.square(self.sigma) * tt.eye(X.shape[0])
        else:
            return tt.zeros(tt.stack([X.shape[0], Xs.shape[0]]))


class Stationary(Covariance):
    R"""
    Base class for stationary kernels/covariance functions.

    Parameters
    ----------
    lengthscales: If input_dim > 1, a list or array of scalars or PyMC3 random
    variables.  If input_dim == 1, a scalar or PyMC3 random variable.
    """

    def __init__(self, input_dim, lengthscales, active_dims=None):
        super(Stationary, self).__init__(input_dim, active_dims)
        self.lengthscales = tt.as_tensor_variable(lengthscales)

    def square_dist(self, X, Xs):
        X = tt.mul(X, 1.0 / self.lengthscales)
        X2 = tt.sum(tt.square(X), 1)
        if Xs is None:
            sqd = (-2.0 * tt.dot(X, tt.transpose(X))
                   + (tt.reshape(X2, (-1, 1)) + tt.reshape(X2, (1, -1))))
        else:
            Xs = tt.mul(Xs, 1.0 / self.lengthscales)
            Xs2 = tt.sum(tt.square(Xs), 1)
            sqd = (-2.0 * tt.dot(X, tt.transpose(Xs))
                   + (tt.reshape(X2, (-1, 1)) + tt.reshape(Xs2, (1, -1))))
        return tt.clip(sqd, 0.0, np.inf)

    def euclidean_dist(self, X, Xs):
        r2 = self.square_dist(X, Xs)
        return tt.sqrt(r2 + 1e-12)

    def diag(self, X):
        return tt.ones(tt.stack([X.shape[0], ]))

    def full(self, X, Xs=None):
        raise NotImplementedError


class ExpQuad(Stationary):
    R"""
    The Exponentiated Quadratic kernel.  Also refered to as the Squared
    Exponential, or Radial Basis Function kernel.

    .. math::

       k(x, x') = \mathrm{exp}\left[ -\frac{(x - x')^2}{2 \ell^2} \right]
    """

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        return tt.exp(-0.5 * self.square_dist(X, Xs))


class RatQuad(Stationary):
    R"""
    The Rational Quadratic kernel.

    .. math::

       k(x, x') = \left(1 + \frac{(x - x')^2}{2\alpha\ell^2} \right)^{-\alpha}
    """

    def __init__(self, input_dim, lengthscales, alpha, active_dims=None):
        super(RatQuad, self).__init__(input_dim, lengthscales, active_dims)
        self.alpha = alpha

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        return (tt.power((1.0 + 0.5 * self.square_dist(X, Xs)
                         * (1.0 / self.alpha)), -1.0 * self.alpha))


class Matern52(Stationary):
    R"""
    The Matern kernel with nu = 5/2.

    .. math::

       k(x, x') = \left(1 + \frac{\sqrt{5(x - x')^2}}{\ell} +
                   \frac{5(x-x')^2}{3\ell^2}\right)
                   \mathrm{exp}\left[ - \frac{\sqrt{5(x - x')^2}}{\ell} \right]
    """

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        r = self.euclidean_dist(X, Xs)
        return ((1.0 + np.sqrt(5.0) * r + 5.0 / 3.0 * tt.square(r))
                * tt.exp(-1.0 * np.sqrt(5.0) * r))


class Matern32(Stationary):
    R"""
    The Matern kernel with nu = 3/2.

    .. math::

       k(x, x') = \left(1 + \frac{\sqrt{3(x - x')^2}}{\ell}\right)
                  \mathrm{exp}\left[ - \frac{\sqrt{3(x - x')^2}}{\ell} \right]
    """

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        r = self.euclidean_dist(X, Xs)
        return (1.0 + np.sqrt(3.0) * r) * tt.exp(-np.sqrt(3.0) * r)


class Exponential(Stationary):
    R"""
    The Exponential kernel.

    .. math::

       k(x, x') = \mathrm{exp}\left[ -\frac{||x - x'||}{2\ell^2} \right]
    """

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        return tt.exp(-0.5 * self.euclidean_dist(X, Xs))


class Cosine(Stationary):
    R"""
    The Cosine kernel.

    .. math::
       k(x, x') = \mathrm{cos}\left( \frac{||x - x'||}{ \ell^2} \right)
    """

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        return tt.cos(np.pi * self.euclidean_dist(X, Xs))


class Linear(Covariance):
    R"""
    The Linear kernel.

    .. math::
       k(x, x') = (x - c)(x' - c)
    """

    def __init__(self, input_dim, c, active_dims=None):
        super(Linear, self).__init__(input_dim, active_dims)
        self.c = c

    def _common(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        Xc = tt.sub(X, self.c)
        return X, Xc, Xs

    def full(self, X, Xs=None):
        X, Xc, Xs = self._common(X, Xs)
        if Xs is None:
            return tt.dot(Xc, tt.transpose(Xc))
        else:
            Xsc = tt.sub(Xs, self.c)
            return tt.dot(Xc, tt.transpose(Xsc))

    def diag(self, X):
        X, Xc, _ = self._common(X, None)
        return tt.sum(tt.square(Xc), 1)


class Polynomial(Linear):
    R"""
    The Polynomial kernel.

    .. math::
       k(x, x') = [(x - c)(x' - c) + \mathrm{offset}]^{d}
    """

    def __init__(self, input_dim, c, d, offset, active_dims=None):
        super(Polynomial, self).__init__(input_dim, c, active_dims)
        self.d = d
        self.offset = offset

    def full(self, X, Xs=None):
        linear = super(Polynomial, self).full(X, Xs)
        return tt.power(linear + self.offset, self.d)

    def diag(self, X):
        linear = super(Polynomial, self).diag(X)
        return tt.power(linear + self.offset, self.d)


class WarpedInput(Covariance):
    R"""
    Warp the inputs of any kernel using an arbitrary function
    defined using Theano.

    .. math::
       k(x, x') = k(w(x), w(x'))

    Parameters
    ----------
    cov_func : Covariance
    warp_func : callable
        Theano function of X and additional optional arguments.
    args : optional, tuple or list of scalars or PyMC3 variables
        Additional inputs (besides X or Xs) to warp_func.
    """

    def __init__(self, input_dim, cov_func, warp_func, args=None,
                 active_dims=None):
        super(WarpedInput, self).__init__(input_dim, active_dims)
        if not callable(warp_func):
            raise TypeError("warp_func must be callable")
        if not isinstance(cov_func, Covariance):
            raise TypeError("Must be or inherit from the Covariance class")
        self.w = handle_args(warp_func, args)
        self.args = args
        self.cov_func = cov_func

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        if Xs is None:
            return self.cov_func(self.w(X, self.args), Xs)
        else:
            return self.cov_func(self.w(X, self.args), self.w(Xs, self.args))

    def diag(self, X):
        X, _ = self._slice(X, None)
        return self.cov_func(self.w(X, self.args), diag=True)


class Gibbs(Covariance):
    R"""
    The Gibbs kernel.  Use an arbitrary lengthscale function defined
    using Theano.  Only tested in one dimension.

    .. math::
       k(x, x') = \sqrt{\frac{2\ell(x)\ell(x')}{\ell^2(x) + \ell^2(x')}}
                  \mathrm{exp}\left[ -\frac{(x - x')^2}
                                           {\ell^2(x) + \ell^2(x')} \right]

    Parameters
    ----------
    lengthscale_func : callable
        Theano function of X and additional optional arguments.
    args : optional, tuple or list of scalars or PyMC3 variables
        Additional inputs (besides X or Xs) to lengthscale_func.
    """

    def __init__(self, input_dim, lengthscale_func, args=None,
                 active_dims=None):
        super(Gibbs, self).__init__(input_dim, active_dims)
        if active_dims is not None:
            if input_dim != 1 or sum(active_dims) == 1:
                raise NotImplementedError(("Higher dimensional inputs ",
                                           "are untested"))
        else:
            if input_dim != 1:
                raise NotImplementedError(("Higher dimensional inputs ",
                                           "are untested"))
        if not callable(lengthscale_func):
            raise TypeError("lengthscale_func must be callable")
        self.lfunc = handle_args(lengthscale_func, args)
        self.args = args

    def square_dist(self, X, Xs):
        X = tt.as_tensor_variable(X)
        X2 = tt.sum(tt.square(X), 1)
        if Xs is None:
            sqd = (-2.0 * tt.dot(X, tt.transpose(X))
                   + (tt.reshape(X2, (-1, 1)) + tt.reshape(X2, (1, -1))))
        else:
            Xs = tt.as_tensor_variable(Xs)
            Xs2 = tt.sum(tt.square(Xs), 1)
            sqd = (-2.0 * tt.dot(X, tt.transpose(Xs))
                   + (tt.reshape(Xs2, (-1, 1)) + tt.reshape(Xs2, (1, -1))))
        return tt.clip(sqd, 0.0, np.inf)

    def full(self, X, Xs=None):
        X, Xs = self._slice(X, Xs)
        rx = self.lfunc(X, self.args)
        rx2 = tt.reshape(tt.square(rx), (-1, 1))
        if Xs is None:
            r2 = self.square_dist(X, X)
            rz = self.lfunc(X, self.args)
        else:
            r2 = self.square_dist(X, Xs)
            rz = self.lfunc(Xs, self.args)
        rz2 = tt.reshape(tt.square(rz), (1, -1))
        return (tt.sqrt((2.0 * tt.dot(rx, tt.transpose(rz))) / (rx2 + rz2))
                * tt.exp(-1.0 * r2 / (rx2 + rz2)))

    def diag(self, X):
        return tt.ones(tt.stack([X.shape[0], ]))


def handle_args(func, args):
    def f(x, args):
        if args is None:
            return func(x)
        else:
            if not isinstance(args, tuple):
                args = (args,)
            return func(x, *args)
    return f
