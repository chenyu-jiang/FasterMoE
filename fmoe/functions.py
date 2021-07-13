r"""
The fmoe.functions module contains functions that are directly warped up from
C/CUDA functions to complete distributed communication, computation and gradient
computation.
"""

import torch
from torch.autograd import Function
import fmoe_cuda
from .utils import get_torch_default_comm
from copy import deepcopy

def _ensure_nccl(t, comm=None):
    if comm is None:
        comm = get_torch_default_comm()
    fmoe_cuda.ensure_nccl(comm, t)


def count_by_gate(gate, num_expert, world_size, require_pos=True):
    with torch.no_grad():
        local_expert_count = torch.zeros(
            num_expert * world_size, device=gate.device, dtype=torch.int32
        )
        fmoe_cuda.expert_count(gate, local_expert_count)
        local_expert_count = local_expert_count.long()

        if world_size > 1:
            _ensure_nccl(gate)
            global_expert_count = fmoe_cuda.expert_exchange(
                local_expert_count, num_expert, world_size
            )
        else:
            global_expert_count = local_expert_count
        if not require_pos:
            pos = None
        else:
            lec_cum = torch.cumsum(local_expert_count, dim=0).int()
            pos_size = lec_cum[-1].item()
            pos = torch.empty((pos_size,), device=gate.device, dtype=torch.long)
            fmoe_cuda.assign_pos(lec_cum, gate, pos)
    return pos, local_expert_count, global_expert_count


def prepare_forward(gate, num_expert, world_size, comm=None):
    r"""
    Prepare necessary information from gate output for MoE computation.

    Args:
        gate: a 1-d Long Tensor representing the target expert of each input
        sample.
        num_expert: number of experts on each worker.
        world_size: number of workers that hold different experts.
        comm: the communicator of all workers in the expert-parallel group.
    """
    if world_size > 1:
        _ensure_nccl(gate, comm=comm)

    pos, local_expert_count, global_expert_count = count_by_gate(gate, 
            num_expert, world_size)
    with torch.no_grad():
        fwd_expert_count = global_expert_count.view(world_size,
                num_expert).sum(dim=0)
        fwd_batch_size = int(fwd_expert_count.sum().item())
    return (
        pos,
        local_expert_count.cpu(),
        global_expert_count.cpu(),
        fwd_expert_count.cpu(),
        fwd_batch_size,
    )


def _local_scatter(inp, pos):
    inp_buf = torch.index_select(inp, 0, pos)
    return inp_buf


def _local_gather(inp, pos, out_batch_size, maybe_overlap=True):
    inp_buf = torch.zeros(out_batch_size, inp.shape[-1],
            dtype=inp.dtype, device=inp.device)
    if maybe_overlap:
        inp_buf.index_add_(0, pos, inp)
    else:
        inp_buf.index_copy_(0, pos, inp)
    return inp_buf

class MOECache(Function):
    r"""
    Applies a caching according to a policy function
    """

    @staticmethod
    def forward(
        ctx, 
        local_expert_count,
        global_expert_count,
        fwd_expert_count,
        policy_fn,
        experts,
        batch_size, 
        topk,
        num_expert, 
        world_size,
        fused, # for optimization
    ):
        if world_size > 1:
            selection = policy_fn(fwd_expert_count, global_expert_count, batch_size, topk)
            
            # sent_models is the information of which models to send and to where according to the previous selection. stored_models will contain the info of where to fetch models from
            sent_models, stored_models = [x.cpu() for x in fmoe_cuda.exchange_cache_info(selection.cuda(), num_expert, world_size)]

            local_params, all_params, models = MOECache._generate_model_parameters(sent_models, stored_models, experts, fused)
            
            # print('Before params:', all_params)

            if not fused:
                fmoe_cuda.model_exchange(sent_models, stored_models, local_params, all_params, num_expert, world_size, fused)
            else:
                sent_models = sent_models.any(dim=1)
                stored_models =  stored_models.any(dim=1)
                
                fmoe_cuda.model_exchange(sent_models, stored_models, local_params, all_params, 1, world_size, fused)

                # Because we are fusing, all other experts will be sent together
                sent_models = sent_models.view(world_size, 1).repeat(1, num_expert)
                stored_models = stored_models.view(world_size, 1).repeat(1, num_expert)
                
            # print('After params:', all_params)
            
            return models, sent_models, stored_models
        else:
            # TODO what?
            raise NotImplementedError

    @staticmethod
    def _generate_model_parameters(sent_models, stored_models, experts, fused):
        r"""
        TODO this function assumes all nodes have the same experts
        """
        if fused:
            local_params = [list(experts.parameters())]

            fetch_models = [
                [deepcopy(experts)]
                if i.any() else []
                for i in stored_models
            ]

        else:
            raise NotImplementedError('No fused yet')

        fetch_params = [[list(m.parameters()) for m in node] for node in fetch_models]
        return local_params, fetch_params, fetch_models

    @staticmethod
    def backward(ctx):
        pass

class MOEScatter(Function):
    r"""
    Scatter input samples from [batch x sequences] to contiguous alone experts.
    If `world_size` is greater than 1, the samples will first be locally
    scattered, and then exchanged across workers.
    """

    @staticmethod
    def forward(
        ctx,
        inp,
        pos,
        local_expert_count,
        global_expert_count,
        sent_models,
        stored_models,
        fwd_batch_size,
        world_size,
    ):
        local_input_buf = _local_scatter(inp, pos)
        if world_size > 1:
            # FIXME remove fwd_batch_size since we won't use it
            staying = int((local_expert_count.view(world_size, -1) * stored_models).sum().item())
            not_arriving = int((global_expert_count.view(world_size, -1) * sent_models).sum().item())
            print(f'Updating fwd_batch size from {fwd_batch_size}, staying {staying}, not_arriving {not_arriving}')
            fwd_batch_size += staying - not_arriving
            print(local_input_buf)
            global_input_buf = fmoe_cuda.global_scatter(
                local_input_buf,
                local_expert_count,
                global_expert_count,
                sent_models,
                stored_models,
                fwd_batch_size,
                world_size,
            )
            print(global_input_buf)
        else:
            global_input_buf = local_input_buf
        ctx.moe_args = inp.shape[0], pos.shape[0], world_size
        variables = (pos, local_expert_count, global_expert_count, sent_models, stored_models)
        ctx.save_for_backward(*variables)
        return global_input_buf

    @staticmethod
    def backward(ctx, global_grad_in):
        (pos, local_expert_count, global_expert_count, sent_models, stored_models) = ctx.saved_tensors
        (inp_batch_size, buf_batch_size, world_size) = ctx.moe_args

        if world_size > 1:
            local_grad_in = fmoe_cuda.global_gather(
                global_grad_in,
                local_expert_count,
                global_expert_count,
                sent_models,
                stored_models,
                buf_batch_size,
                world_size,
            )
        else:
            local_grad_in = global_grad_in
        grad_in = _local_gather(local_grad_in, pos, inp_batch_size)
        return grad_in, None, None, None, None, None, None, None


class MOELinear(Function):
    r"""
    Computes linear operators within one GPU on different experts simutaneously.
    """

    @staticmethod
    def forward(ctx, global_input_buf, fwd_expert_count, weight, bias=None):
        global_output_buf = fmoe_cuda.linear_forward(
            global_input_buf, fwd_expert_count, weight, bias
        )
        variables = (global_input_buf, fwd_expert_count, weight, bias)
        ctx.save_for_backward(*variables)
        return global_output_buf

    @staticmethod
    def backward(ctx, grad_out):
        (input_buf, fwd_expert_count, weight, bias) = ctx.saved_tensors
        grad_inp_buf, grad_weight, grad_bias = fmoe_cuda.linear_backward(
            grad_out, input_buf, fwd_expert_count, weight, bias
        )

        if not torch.is_tensor(bias):
            grad_bias = None

        return grad_inp_buf, None, grad_weight, grad_bias


class MOEGather(Function):
    r"""
    Gather output samples from contiguous alone experts back to [batch x
    sequences]. Works symmetrically with MOEScatter.
    """

    @staticmethod
    def forward(
        ctx,
        global_output_buf,
        pos,
        local_expert_count,
        global_expert_count,
        sent_models,
        stored_models,
        local_batch_size,
        world_size,
    ):
        if world_size > 1:
            print(global_output_buf)
            local_output_buf = fmoe_cuda.global_gather(
                global_output_buf,
                local_expert_count,
                global_expert_count,
                sent_models,
                stored_models,
                pos.shape[0],
                world_size,
            )
            print(local_output_buf)
        else:
            local_output_buf = global_output_buf
        output = _local_gather(local_output_buf, pos, local_batch_size,
                maybe_overlap=False)

        ctx.moe_args = (global_output_buf.shape[0], world_size)
        variables = (pos, local_expert_count, global_expert_count, sent_models, stored_models)
        ctx.save_for_backward(*variables)
        return output

    @staticmethod
    def backward(ctx, grad_out):
        pos, local_expert_count, global_expert_count, sent_models, stored_models = ctx.saved_tensors
        fwd_batch_size, world_size = ctx.moe_args
        grad_out_buf = _local_scatter(grad_out.contiguous(), pos)
        if world_size > 1:
            global_grad_out_buf = fmoe_cuda.global_scatter(
                grad_out_buf,
                local_expert_count,
                global_expert_count,
                sent_models,
                stored_models,
                fwd_batch_size,
                world_size,
            )
        else:
            global_grad_out_buf = grad_out_buf
        return global_grad_out_buf, None, None, None, None, None, None, None


class AllGather(Function):
    r"""
    A wrapper for the All-Gather function to support auto-differentiation.
    """

    @staticmethod
    def forward(ctx, inp, rank, world_size, group):
        tensor_list = [torch.empty_like(inp) for _ in range(world_size)]
        torch.distributed.all_gather(tensor_list, inp, group=group)
        torch.cuda.synchronize()
        output = torch.cat(tensor_list, dim=0)
        ctx.args = rank, inp.shape[0]
        return output

    @staticmethod
    def backward(ctx, grad_out):
        rank, dim0 = ctx.args
        return grad_out[rank * dim0 : (rank + 1) * dim0], None, None, None


class Slice(Function):
    r"""
    A wrapper for the Slice function to support auto-differentiation.
    """

    @staticmethod
    def forward(ctx, inp, rank, world_size, group):
        B: int = inp.shape[0]
        local_batch_size = B // world_size
        batch_start = local_batch_size * rank
        batch_end = min(batch_start + local_batch_size, B)
        inp = inp[batch_start:batch_end]
        ctx.args = world_size, group
        return inp

    @staticmethod
    def backward(ctx, grad_out):
        world_size, group = ctx.args
        tensor_list = [torch.empty_like(grad_out) for _ in range(world_size)]
        torch.distributed.all_gather(tensor_list, grad_out, group=group)
        torch.cuda.synchronize()
        grad_out = torch.cat(tensor_list, dim=0)
        return grad_out, None, None, None
