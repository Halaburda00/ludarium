# ADR-0009 fastembed instead of sentence-transformers

Status: accepted, 2026-08-10

## Context

The matcher uses embeddings to retrieve candidate pairs. The canonical way to
produce them in Python is `sentence-transformers`, which pulls in `torch`.

`torch` is larger than everything else in this project combined, and the target
is a container that runs comfortably on a NAS, built for `linux/arm64` as well
as `linux/amd64`, with an image budget under 500 MB. That budget is not a
preference; it is what makes the deployment story in M5 true.

## Decision

Embeddings are produced by `fastembed` on ONNX Runtime, which serves the same
models at a fraction of the install size. **`torch` and `sentence-transformers`
are never added to this project.**

The embedding is a retriever, not a decider: it generates candidates that a
feature-based classifier adjudicates (ADR-0007). The document embedded is always
composite — title, year, publisher, platforms — never a bare title, or *Prey*
(2006) and *Prey* (2017) collapse into one record. `work_embedding` stores the
model name and version alongside the vector.

Alternatives considered:

- **`sentence-transformers` with `torch`.** The reference implementation, the
  widest model selection, GPU support. Rejected on size alone: it would multiply
  the image and make the ARM build painful, in exchange for capabilities a
  retriever over a few thousand short documents does not need.
- **A hosted embedding API.** No local weights at all, and always the newest
  models. Rejected: it requires a key and network access for a core function,
  and sends the user's library to a third party — unacceptable in a self-hosted
  tool.

## Consequences

- The image stays inside its budget, the `arm64` build works, and no GPU is
  assumed anywhere.
- The model choice is limited to what is exported to ONNX. A model that is not
  will simply be unavailable, with no workaround short of reversing this
  decision.
- Inference is CPU-only, so embedding a large library is slow enough that it
  belongs in a background job rather than in a request.
- `model_name` and `model_version` are stored per vector because changing either
  invalidates the whole index. Without them, a model upgrade would silently
  compare vectors from different spaces.
