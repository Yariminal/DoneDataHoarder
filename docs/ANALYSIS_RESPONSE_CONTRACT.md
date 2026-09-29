# Analysis response contract

File analyzers pass a Pydantic `model_cls` to `generate_json`. The provider
validates each model response inside its existing three-attempt retry loop.
Only a complete, validated answer reaches `AnalysisResult.from_ai_response`.
The contract applies to image, text and rendered-PDF documents, video, audio,
archives, and 3D model analysis. Relation and proposal JSON consumers retain
their existing parsing behavior.

Every analysis answer is one JSON object with these required fields:

| Field | Required value |
| --- | --- |
| `description` | Nonblank string |
| `suggested_name` | Nonblank string |
| `tags` | Array of strings; an empty array is allowed when no tag is supported, but blank or non-string elements are rejected |
| `confidence` | JSON number from 0 through 1, finite and not a boolean |

Prompt-specific fields are also required. Image: `category` and
`detected_date`; document and rendered PDF: `document_type`, `detected_date`,
and `language`; video: `video_type` and `detected_date`; audio: `audio_type`
and `detected_date`; archive: `archive_type` and `detected_date`; 3D model:
`asset_type` and `software`. Type/category values follow the choices in their
prompts. `detected_date` is a real `YYYY-MM-DD` date or explicit `null` when
unknown. `language` is a lowercase two-letter ISO 639-1 code or `null` for
graphical pages; `software` is a prompt-listed value or `null` when unknown.
Missing fields, placeholders of the wrong type, and out-of-range confidence
retry instead of becoming default values. Extra keys are rejected. In
particular, legacy `date`, `transcript`, and `_list` keys cannot bypass the
typed fields or alter the provider's result shape.

The analysis parser accepts a single complete JSON object, a complete
Markdown `json` code fence, or a prose introduction ending in a colon followed by
that object. Only whitespace may follow the object. A second object, partial
trailing value, malformed fence, duplicate key, or truncated outer object
retries. The bounded object-key comma repair still applies only when the whole
answer parses after repair and passes the schema. Generic `extract_json`
continues to accept first-value extraction for other callers.

After retry exhaustion, the analyzer reports a provider failure. The pipeline
records `ERROR` and `analysis_outcome=failed` with no AI description, tags,
confidence, model, or inferred date. The file remains eligible for the
explicit analysis error retry and cannot generate an evidence-backed proposal.
The analysis prompt/cache version is `analysis-v4-2026-09-29`; earlier cache
entries are reanalyzed under this contract rather than restored.
