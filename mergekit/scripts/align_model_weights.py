# Copyright (C) 2024 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

import logging
from typing import List, Optional

import click
import numpy as np
import ot
import torch
import tqdm
from scipy.optimize import linear_sum_assignment

from mergekit.common import ModelReference
from mergekit.options import MergeOptions, add_merge_options
from mergekit.permuter import ModelPermuter


def split_heads(
    xs: List[torch.Tensor], head_dim: int, heads_first: bool
) -> List[torch.Tensor]:
    res = []
    for x in xs:
        assert len(x.shape) == 2
        parts = x.view(-1, head_dim, x.shape[1])
        if not heads_first:
            parts = parts.permute(1, 0, 2)
        res.extend([t.squeeze(0) for t in parts.split(1, dim=0)])
    return res


def estimate_theta(x_0: torch.Tensor, x_1: torch.Tensor, head_dim: int) -> torch.Tensor:
    # x_0, x_1 shape: (..., n_heads, seq_len, head_dim)
    q_0_c = torch.complex(x_0[..., : head_dim // 2], x_0[..., head_dim // 2 :])
    q_1_c = torch.complex(x_1[..., : head_dim // 2], x_1[..., head_dim // 2 :])
    ratio = q_1_c / q_0_c

    # average over sequence length
    ratio = ratio.mean(dim=-2)

    theta_approx = torch.angle(ratio)
    return theta_approx


def theta_to_matrix(theta: torch.Tensor, head_dim: int) -> torch.Tensor:
    # theta shape: (n_heads, head_dim // 2)
    n_heads = theta.shape[0]

    theta_p = torch.cat([theta, theta], dim=-1)
    cos_theta = torch.cos(theta_p)
    sin_theta = torch.sin(theta_p)

    P = torch.zeros(n_heads, head_dim, head_dim, device=theta.device, dtype=theta.dtype)
    idx = torch.arange(head_dim // 2)
    P[:, idx, idx] = cos_theta[:, idx]
    P[:, idx, head_dim // 2 + idx] = sin_theta[:, idx]
    P[:, head_dim // 2 + idx, idx] = -sin_theta[:, idx]
    P[:, head_dim // 2 + idx, head_dim // 2 + idx] = cos_theta[:, idx]
    return P


@click.command("mergekit-align-model")
@click.argument("model_path", type=str)
@click.option(
    "--target", "-t", required=True, type=str, help="Target model to align weights to"
)
@click.option("--out-path", "-o", required=True, type=str, help="Output model path")
@click.option("--iters", "-i", type=int, default=10, help="Number of iterations")
@click.option(
    "--sinkhorn/--no-sinkhorn",
    "-s",
    type=bool,
    default=False,
    help="Use Sinkhorn algorithm",
)
@click.option(
    "--sinkhorn-reg",
    type=float,
    default=0.05,
    help="Regularization for Sinkhorn algorithm",
)
@click.option(
    "--dtype",
    type=str,
    default=None,
    help="Data type to convert weights to",
)
@add_merge_options
def main(
    model_path: str,
    out_path: str,
    target: str,
    iters: int,
    sinkhorn: bool,
    sinkhorn_reg: float,
    dtype: Optional[str],
    merge_options: MergeOptions,
):
    model = ModelReference.model_validate(model_path)
    target_model = ModelReference.model_validate(target)

    permuter = ModelPermuter(model, merge_options, dtype=dtype)
    for m in tqdm.tqdm([model, target_model], desc="Preparing models"):
        permuter.loader_cache.get(m)

    head_permutations = {}

    def align_tensors(
        space: str, in_tensors: List[torch.Tensor], target_tensors: List[torch.Tensor]
    ) -> torch.Tensor:
        out_dim = target_tensors[0].shape[0]
        if not all(x.shape[0] == out_dim for x in target_tensors):
            logging.error(f"Output dimension mismatch for space {space}")
            logging.error(f"Target: {target_tensors[0].shape[0]}")
            logging.error(f"Found shapes: {[x.shape for x in target_tensors]}")
            logging.error(
                f"Weight names: {[x.name for x in permuter.space_out_tensors[space]]}"
            )
            raise RuntimeError(f"Output dimension mismatch for space {space}")

        work_dtype = (
            torch.float32
            if target_tensors[0].device.type == "cpu"
            else target_tensors[0].dtype
        )

        if sinkhorn:
            honk_in = torch.cat(in_tensors, dim=1)
            honk_target = torch.cat(target_tensors, dim=1)
            cost_mat = ot.dist(honk_in, honk_target, metric="sqeuclidean")

            mass = (
                torch.ones(out_dim, device=cost_mat.device, dtype=cost_mat.dtype)
                / out_dim
            )
            model_to_base, log = ot.sinkhorn(
                a=mass,
                b=mass,
                M=cost_mat,
                reg=sinkhorn_reg,
                stopThr=1e-6,
                verbose=False,
                log=True,
                method="sinkhorn_log",
            )
            model_to_base *= out_dim
            if log["err"] and log["err"][-1] > 1e-4:
                logging.warning(f"Space {space}:")
                logging.warning(f'niter: {log["niter"]}')
                logging.warning("err: " + str(log["err"][-1]))
        else:
            cost_mat = torch.zeros(
                out_dim, out_dim, device=target_tensors[0].device, dtype=work_dtype
            )
            for x_target, x_model in zip(target_tensors, in_tensors):
                cost_mat += x_target.to(work_dtype) @ x_model.T.to(work_dtype)

            ri, ci = linear_sum_assignment(cost_mat.cpu().numpy(), maximize=True)
            model_to_base = torch.zeros_like(cost_mat, dtype=target_tensors[0].dtype)
            model_to_base[(ri, ci)] = 1

        return model_to_base

    all_space_names = permuter.all_spaces + [
        f"head:{group}" for group in permuter.head_group_weights.keys()
    ]
    for iter_idx in tqdm.tqdm(range(iters), desc="Iterating"):
        change_count = 0
        perm = (
            np.random.permutation(all_space_names)
            if iter_idx > 0
            else list(all_space_names)
        )
        for space in tqdm.tqdm(
            perm,
            leave=False,
            total=len(permuter.all_spaces),
            desc="Aligning spaces",
        ):
            is_head_group = space.startswith("head:")
            if (
                space not in permuter.space_in_tensors
                and space not in permuter.space_out_tensors
                and not is_head_group
            ):
                continue

            in_tensors = permuter.space_tensors(
                model, space, transform_in=True, transform_out=False
            )
            if not in_tensors:
                continue

            in_tensors = [
                in_tensors[key]
                for key in sorted(in_tensors.keys(), key=lambda x: x.name)
            ]

            target_tensors = permuter.space_tensors(
                target_model, space, transform_in=False, transform_out=False
            )
            wis = target_tensors.keys()
            target_tensors = [
                target_tensors[key]
                for key in sorted(target_tensors.keys(), key=lambda x: x.name)
            ]
            assert len(in_tensors) == len(target_tensors)

            if is_head_group:
                in_heads = split_heads(in_tensors, permuter.head_dim, heads_first=False)
                target_heads = split_heads(
                    target_tensors, permuter.head_dim, heads_first=False
                )

                P_heads = align_tensors(space, in_heads, target_heads)
                old_transform = head_permutations.get(space, None)
                head_permutations[space] = P_heads

                if old_transform is None or not torch.allclose(old_transform, P_heads):
                    change_count += 1
                continue

            head_group = None
            rope = None
            for weight_info in wis:
                if weight_info.head_group:
                    if head_group is not None and head_group != weight_info.head_group:
                        raise RuntimeError(
                            f"Space {space} contains multiple head groups"
                        )
                    head_group = weight_info.head_group
                if rope is None:
                    rope = weight_info.rope
                elif rope != weight_info.rope:
                    print(f"{space}: {rope} vs {weight_info.rope} for {weight_info}")
                    raise RuntimeError(
                        f"Space {space} contains tensors with multiple RoPE statuses"
                    )

            if head_group:
                num_heads = in_tensors[0].shape[0] // permuter.head_dim
                P_heads = head_permutations.get(f"head:{head_group}", None)
                if P_heads is None:
                    P_heads = torch.eye(num_heads, device=in_tensors[0].device)

                expanded = torch.kron(
                    torch.eye(permuter.head_dim, device=in_tensors[0].device),
                    P_heads,
                )

                x_in = (
                    torch.stack(
                        [
                            (expanded @ x).view(-1, permuter.head_dim, x.shape[-1])
                            for x in in_tensors
                        ],
                        dim=0,
                    )
                    .permute(1, 0, 3, 2)
                    .reshape(num_heads, -1, permuter.head_dim)
                )
                x_target = (
                    torch.stack(
                        [
                            x.view(-1, permuter.head_dim, x.shape[-1])
                            for x in target_tensors
                        ],
                        dim=0,
                    )
                    .permute(1, 0, 3, 2)
                    .reshape(num_heads, -1, permuter.head_dim)
                )
                theta = estimate_theta(
                    x_in,
                    x_target,
                    permuter.head_dim,
                )
                P_head_dim = theta_to_matrix(-theta, permuter.head_dim)

                out_dim = in_tensors[0].shape[0]
                P = torch.zeros(
                    out_dim,
                    out_dim,
                    device=in_tensors[0].device,
                    dtype=in_tensors[0].dtype,
                )
                for head_idx in range(num_heads):
                    new_head_idx = torch.argmax(P_heads[head_idx])
                    P[
                        head_idx
                        * permuter.head_dim : (head_idx + 1)
                        * permuter.head_dim,
                        new_head_idx
                        * permuter.head_dim : (new_head_idx + 1)
                        * permuter.head_dim,
                    ] = P_head_dim[head_idx]
                model_to_base = P
            else:
                model_to_base = align_tensors(space, in_tensors, target_tensors)

            old_transform = permuter.transforms.get(space, None)
            base_to_model = None if sinkhorn else model_to_base.T
            permuter.set_transform(space, model_to_base, inverse=base_to_model)

            if old_transform is None or not torch.allclose(
                old_transform, model_to_base
            ):
                if not torch.allclose(
                    model_to_base,
                    torch.eye(
                        model_to_base.shape[0],
                        model_to_base.shape[1],
                        device=model_to_base.device,
                        dtype=model_to_base.dtype,
                    ),
                ):
                    change_count += 1
                for proc in permuter.space_proc_refs[space]:
                    permuter.update_proc_space(proc)
        logging.warning(f"Iteration {iter_idx}: {change_count} changes")

    for space_name in permuter.all_spaces:
        if space_name not in permuter.transforms:
            logging.warning(f"Space {space_name} not transformed")

    permuter.write_permuted_model(out_path)


if __name__ == "__main__":
    with torch.no_grad():
        main()
