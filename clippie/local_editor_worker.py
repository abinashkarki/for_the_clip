"""One bounded offline inference. Imported only inside the isolated worker."""
import json
import sys
import time
from contextlib import redirect_stdout
from importlib.metadata import version


def infer(request):
    from mlx_lm import load, stream_generate
    from mlx_lm.sample_utils import make_sampler
    started = time.monotonic()
    max_tokens = request.get('max_tokens', 1200)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 1200:
        raise ValueError('Local output budget must be from 1 to 1200 tokens.')
    model, tokenizer = load(request['model_path'], trust_remote_code=False)
    prompt = tokenizer.apply_chat_template(request['messages'], add_generation_prompt=True,
                                           tokenize=True, enable_thinking=False)
    if len(prompt) > 18000:
        raise ValueError('Editorial candidate context exceeds the local 18000-token limit.')
    text = []
    last = None
    for response in stream_generate(model, tokenizer, prompt, max_tokens=max_tokens,
            sampler=make_sampler(temp=0), prefill_step_size=512,
            kv_bits=8, kv_group_size=64, quantized_kv_start=4096):
        text.append(response.text); last = response
    if last is None:
        raise ValueError('Local editor returned no tokens.')
    return {'text': ''.join(text), 'usage': {'prompt_tokens': last.prompt_tokens,
          'completion_tokens': last.generation_tokens, 'total_tokens': last.prompt_tokens+last.generation_tokens},
          'metrics': {'seconds': time.monotonic()-started, 'peak_memory_gb': last.peak_memory,
                      'generation_tps': last.generation_tps}, 'finish_reason': last.finish_reason,
          'settings': {'runtime': version('mlx-lm'), 'temperature': 0, 'max_tokens': max_tokens,
                       'kv_bits': 8, 'quantized_kv_start': 4096, 'prefill_step_size': 512}}


def main():
    request = json.load(sys.stdin)
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    from .telemetry import _span_row
    provider = TracerProvider(shutdown_on_exit=False)
    parent = TraceContextTextMapPropagator().extract({'traceparent': request.get('traceparent') or ''})
    span = provider.get_tracer('clippie.model-worker').start_span('model_worker', context=parent)
    with redirect_stdout(sys.stderr):
        result = infer(request)
    span.end()
    if request.get('traceparent'):
        result['telemetry_span'] = _span_row(span)
    provider.shutdown()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
