# Copyright (c) 2021, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause

# Salesforce Research (Junwen Bai, Weiran Wang)

"""
This implementation is modified from https://github.com/BouchardLab/DynamicalComponentsAnalysis/tree/master/dca

*** License Agreement ***

Dynamical Components Analysis (DCA) Copyright (c) 2021, The
Regents of the University of California, through Lawrence Berkeley
National Laboratory (subject to receipt of any required approvals
from the U.S. Dept. of Energy). All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

(1) Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

(2) Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

(3) Neither the name of the University of California, Lawrence Berkeley
National Laboratory, U.S. Dept. of Energy nor the names of its contributors
may be used to endorse or promote products derived from this software
without specific prior written permission.


THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

You are under no obligation whatsoever to provide any bug fixes, patches,
or upgrades to the features, functionality or performance of the source
code ("Enhancements") to anyone; however, if you choose to make your
Enhancements available either publicly, or directly to Lawrence Berkeley
National Laboratory, without imposing a separate written license agreement
for such Enhancements, then you hereby grant the following license: a
non-exclusive, royalty-free perpetual license to install, use, modify,
prepare derivative works, incorporate into other computer software,
distribute, and sublicense such enhancements or derivative works thereof,
in binary and source code form.

"""

import numpy as np
import torch
import scipy as sp
import pdb

def matrix_toeplitzify(cov, T, d):
    """
    # I tested that the two functions are equivalent, with following tests.

    import torch
    from dapc.cov_utils import torch_toeplitzify, matrix_toeplitzify
    X=torch.FloatTensor(100, 10).uniform_(0, 1)
    cov=torch.mm(X.t(), X)/100
    # cov=torch.FloatTensor(10, 10).uniform_(0, 1)

    a=torch_toeplitzify(cov, 10, 1)
    b=matrix_toeplitzify(cov, 10, 1)
    torch.sum(torch.abs(a-b))

    c=torch_toeplitzify(cov, 5, 2)
    d=matrix_toeplitzify(cov, 5, 2)
    torch.sum(torch.abs(c-d))

    e=torch_toeplitzify(cov, 2, 5)
    f=matrix_toeplitzify(cov, 2, 5)
    torch.sum(torch.abs(e-f))
    """

    # First make sure it is symmetric.
    cov = (cov + cov.t()) / 2.0

    cov = cov.reshape(T, d, T, d).permute(1, 3, 0, 2).reshape(d*d, T*T)
    cov = torch.cat([cov, torch.zeros([d*d, T], dtype=cov.dtype, device=cov.device)], 1)

    indicator = torch.ones([T+1, T]).triu().reshape(1, (T+1)*T).repeat(d*d, 1).to(cov.device)
    cov_unfold = cov.unfold(1, T+1, T+1)
    ind_unfold = indicator.unfold(1, T+1, T+1)
    avg = torch.sum(cov_unfold[:, :, :-1] * ind_unfold[:, :, :-1], 1, keepdim=True) / torch.sum(ind_unfold[:, :, :-1], 1, keepdim=True)
    avg = torch.cat([avg, torch.zeros([d*d, 1, 1], dtype=avg.dtype, device=avg.device)], 2) * ind_unfold
    avg = torch.reshape(avg.reshape(d*d, (T+1)*T)[:, :(T*T)], [d, d, T, T])

    indicator = torch.ones([T, T], dtype=avg.dtype, device=avg.device).triu().reshape(1, 1, T, T)
    # Full transpose in original space.
    result = (avg + avg.permute(1, 0, 3, 2)) / (indicator + indicator.transpose(2, 3))
    result = result.permute(2, 0, 3, 1).reshape(T*d, T*d)
    return result


def rectify_spectrum(cov, epsilon=1e-6, verbose=False):
    """Rectify the spectrum of a covariance matrix.

    Parameters
    ----------
    cov : ndarray
        Covariance matrix
    epsilon : float
        Minimum eigenvalue for the rectified spectrum.
    verbose : bool
        Whethere to print when the spectrum needs to be rectified.
    """
    min_eig = np.min(sp.linalg.eigvalsh(cov.detach().cpu().numpy()))
    if min_eig < 0:
        cov += (-min_eig + epsilon) * torch.eye(cov.shape[0])
        if verbose:
            print("Warning: non-PSD matrix (had to increase eigenvalues)")


def calc_pi_from_cov(cov_2_T_pi):
    """Calculates the mutual information ("predictive information"
    or "PI") between variables  {1,...,T_pi} and {T_pi+1,...,2*T_pi}, which
    are jointly Gaussian with covariance matrix cov_2_T_pi.

    Parameters
    ----------
    cov_2_T_pi : np.ndarray, shape (2*T_pi, 2*T_pi)
        Covariance matrix.

    Returns
    -------
    PI : float
        Mutual information in nats.
    """
    T_pi = cov_2_T_pi.shape[0] // 2

    cov_T_pi = cov_2_T_pi[:T_pi, :T_pi]
    logdet_T_pi = torch.logdet(cov_T_pi)
    logdet_2T_pi = torch.logdet(cov_2_T_pi)

    PI = logdet_T_pi - .5 * logdet_2T_pi
    return PI


def calc_cov_from_data(xs_pad, src_mask, T, toeplitzify=True, reg=0.0):
    """Compute the TN-by-TN cross-covariance matrix, where d is the data dimensionality,
    for each time lag up to T-1.

    Parameters
    ----------
    X : shape (batch, maxlen, d)
        The d-dimensional time series data from which the covariance matrices are computed.
    src_mask: shape (batch, maxlen), type: bool
        The length of each sequence in X
    T : int
        The number of time lags.

    Returns
    -------
    cov_est : shape (TN, TN), float
        Covariance matrices for length T time steps. cov_est[t1, t2] is the cross-covariance between
        X(t1) and X(t2), where X(t) is an d-dimensional vector.
    """

    B = xs_pad.size(0)
    maxlen = xs_pad.size(1)
    d = xs_pad.size(2)
    T = int(T)

    if torch.min(src_mask.sum(1)) <= T:
        raise ValueError('T must be shorter than the length of the shortest ' +
                         'time series. If you are using the DCA model, 2 * DCA.T must be ' +
                         'shorter than the shortest time series.')

    # Extracts sliding local blocks.
    xs_with_lags = xs_pad.view([B, maxlen*d]).unfold(1, T*d, d)
    # Find the valid concat frames.
    mask_with_lags = src_mask.unfold(1, T, 1).all(dim=2)
    mask_float = mask_with_lags.float().flatten().unsqueeze(1)
    xs_with_lags = torch.reshape(xs_with_lags, [-1, T*d])
    xs_with_lags_mean = torch.sum(torch.mul(xs_with_lags, mask_float), 0, keepdim=True) / torch.sum(mask_float)
    # Remove mean for concat frames.
    xs_with_lags = xs_with_lags - xs_with_lags_mean

    cov_est = torch.mul(xs_with_lags, mask_float).t().matmul(xs_with_lags) / torch.sum(mask_float)
    if toeplitzify:
        cov_est = matrix_toeplitzify(cov_est, T, d)

    if reg>0:
        cov_est = cov_est + reg * torch.eye(T*d, T*d, device=cov_est.device)

    return cov_est
