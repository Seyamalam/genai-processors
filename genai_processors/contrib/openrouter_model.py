# Copyright 2025 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Wraps the OpenRouter API into a Processor.

This module provides access to hundreds of AI models through OpenRouter's
unified API. OpenRouter automatically handles fallbacks and selects the most
cost-effective options while providing access to models from OpenAI, Anthropic,
Google, Meta, and many other providers.

## Example usage

```py
p = OpenRouterModel(
    api_key="your-openrouter-api-key",
    model_name="google/gemma-3-27b-it",
    generate_content_config=GenerateContentConfig(
        temperature=0.7,
        max_tokens=1000,
    )
)
```

### Sync Execution

```py
INPUT_PROMPT = 'Write a haiku about artificial intelligence'

content = processors.apply_sync(p, [INPUT_PROMPT])
for part in content:
  if part.text:
    print(part.text)
```

### Async Execution

```py
async for part in p.stream_content([INPUT_PROMPT]):
  if part.text:
    print(part.text)
```

### Available Models

OpenRouter provides access to hundreds of models including:
- OpenAI: gpt-4o, gpt-4-turbo, gpt-3.5-turbo
- Anthropic: claude-3-5-sonnet, claude-3-opus, claude-3-haiku
- Google: gemma-3-27b-it, gemini-2.5-pro
- Meta: llama-3.3-70b-instruct, llama-4-maverick
- And many more...

For a complete list, visit: https://openrouter.ai/models
"""

from collections.abc import AsyncIterable
import json
from typing import Any, Literal

from genai_processors import content_api
from genai_processors import processor
from google.genai import _transformers
from google.genai import types as genai_types
import httpx
from typing_extensions import TypedDict


_DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'
_DEFAULT_TIMEOUT = 300


class OpenRouterAPIError(Exception):
  """Base exception for OpenRouter API errors."""
  pass


class AuthenticationError(OpenRouterAPIError):
  """Raised when API authentication fails."""
  pass


class RateLimitError(OpenRouterAPIError):
  """Raised when API rate limit is exceeded."""
  pass


class GenerateContentConfig(TypedDict, total=False):
  """Optional model configuration parameters for OpenRouter."""

  temperature: float | None
  """Controls randomness in the response. Range: 0.0 to 2.0"""

  top_p: float | None
  """Controls diversity via nucleus sampling. Range: 0.0 to 1.0"""

  top_k: int | None
  """Limits the number of tokens to consider for each step."""

  frequency_penalty: float | None
  """Reduces repetition. Range: -2.0 to 2.0"""

  presence_penalty: float | None
  """Reduces repetition based on token presence. Range: -2.0 to 2.0"""

  repetition_penalty: float | None
  """Alternative to frequency_penalty. Range: 0.0 to 2.0"""

  min_p: float | None
  """Alternative to top_p. Minimum probability threshold."""

  top_a: float | None
  """Alternative to top_p. Top-a sampling parameter."""

  seed: int | None
  """Random seed for deterministic outputs."""

  max_tokens: int | None
  """Maximum number of tokens to generate."""

  response_schema: genai_types.SchemaUnion | None
  """The `Schema` object allows the definition of input and output data types.

  These types can be objects, but also primitives and arrays.
  Represents a select subset of an [OpenAPI 3.0 schema
  object](https://spec.openapis.org/oas/v3.0.3#schema).
  If set, a compatible response_mime_type must also be set.
  Compatible mimetypes: `application/json`: Schema for JSON response.
  """

  stop: list[str] | str | None
  """Stop sequences to end generation."""

  tools: list[genai_types.Tool] | None
  """Function calling tools available to the model."""

  tool_choice: str | dict[str, Any] | None
  """How the model should use tools ("auto", "none", specific tool)"""

  logit_bias: dict[str, float] | None
  """Modify likelihood of specific tokens."""

  transforms: list[str] | None
  """OpenRouter-specific transforms to apply."""

  models: list[str] | None
  """Fallback models if primary model fails."""

  route: Literal['fallback'] | None
  """Routing strategy for model selection."""

  provider: dict[str, Any] | None
  """Provider-specific preferences."""


def _to_openrouter_message(
    part: content_api.ProcessorPart, default_role: str = 'user'
) -> dict[str, Any]:
  """Convert ProcessorPart to OpenRouter message format."""
  role = part.role.lower() if part.role else default_role

  # Handle function calls
  if part.function_call:
    return {
        'role': part.role.lower(),
        'function_call': {
            'name': part.function_call.name,
            'arguments': json.dumps(part.function_call.args),
        },
    }

  # Handle function responses
  if part.function_response:
    return {
        'role': 'function',
        'name': part.function_response.name,
        'content': json.dumps(part.function_response.response),
    }

  # Handle text content
  if content_api.is_text(part.mimetype):
    return {
        'role': role,
        'content': part.text,
    }

  # Handle multimodal content
  if content_api.is_image(part.mimetype):
    # Convert image to base64 for OpenRouter
    import base64

    if part.bytes:
      encoded_image = base64.b64encode(part.bytes).decode('utf-8')
      return {
          'role': role,
          'content': [
              {
                  'type': 'image_url',
                  'image_url': {
                      'url': f'data:{part.mimetype};base64,{encoded_image}'
                  },
              }
          ],
      }

  # Fail verbosely for unsupported types
  raise ValueError(f'Unsupported Part type: {part.mimetype}')


def _parse_sse_line(line: str) -> dict[str, Any] | None:
  """Parse a Server-Sent Events line."""
  line = line.strip()

  # Skip comments and empty lines
  if not line or line.startswith(':'):
    return None

  # Parse data lines
  if line.startswith('data: '):
    data = line[6:]
    if data == '[DONE]':
      return {'type': 'done'}

    # Let JSON errors propagate - don't suppress them
    return json.loads(data)

  return None


class OpenRouterModel(processor.Processor):
  """`Processor` that calls OpenRouter API with streaming support.

  OpenRouter provides access to hundreds of AI models through a unified API,
  including models from OpenAI, Anthropic, Google, Meta, and many others.
  """

  def __init__(
      self,
      *,
      api_key: str,
      model_name: str,
      base_url: str | None = None,
      site_url: str | None = None,
      site_name: str | None = None,
      generate_content_config: GenerateContentConfig | None = None,
  ):
    """Initialize the OpenRouter model.

    Args:
      api_key: Your OpenRouter API key.
      model_name: Model to use (e.g., "openai/gpt-4o", "anthropic/claude-3-5-sonnet").
      base_url: OpenRouter API base URL (defaults to https://openrouter.ai/api/v1).
      site_url: Your site URL (optional, for OpenRouter rankings).
      site_name: Your site name (optional, for OpenRouter rankings).
      generate_content_config: Model configuration parameters.

    Returns:
      A `Processor` that calls the OpenRouter API with streaming support.
    """
    self._api_key = api_key
    self._model_name = model_name
    self._base_url = base_url or _DEFAULT_BASE_URL
    self._site_url = site_url
    self._site_name = site_name
    self._config = generate_content_config or {}

    # Build headers
    headers = {
        'Authorization': f'Bearer {self._api_key}',
        'Content-Type': 'application/json',
        'User-Agent': 'genai-processors',
    }

    if self._site_url:
      headers['HTTP-Referer'] = self._site_url
    if self._site_name:
      headers['X-Title'] = self._site_name

    self._client = httpx.AsyncClient(
        base_url=self._base_url,
        headers=headers,
        timeout=_DEFAULT_TIMEOUT,
    )

    # Initialize tools
    if tools := self._config.get('tools'):
      self._tools = []
      for tool in tools:
        for tool_name in (
            'retrieval',
            'google_search',
            'google_search_retrieval',
            'enterprise_web_search',
            'google_maps',
            'url_context',
            'code_execution',
            'computer_use',
        ):
          if getattr(tool, tool_name) is not None:
            raise ValueError(f'Tool {tool_name} is not supported.')

        for fdecl in tool.function_declarations or ():
          if fdecl.parameters:
            parameters = _transformers.t_schema(  # pytype: disable=wrong-arg-types
                _FakeClient(), fdecl.parameters
            ).json_schema.model_dump(
                mode='json', exclude_unset=True
            )
          else:
            parameters = None

          self._tools.append({
              'type': 'function',
              'function': {
                  'name': fdecl.name,
                  'description': fdecl.description,
                  'parameters': parameters,
              },
          })
    else:
      self._tools = None

  @property
  def key_prefix(self) -> str:
    """Key prefix for caching."""
    return f'OpenRouterModel_{self._model_name}'

  def _parse_error_response(self, error_body: bytes) -> str:
    """Parse error details from API response.

    Args:
        error_body: Raw error response body from the API.

    Returns:
        Human-readable error message extracted from the response.
    """
    try:
      error_data = json.loads(error_body)
      # OpenRouter typically returns errors in {"error": {"message": "..."}} format
      return error_data.get('error', {}).get('message', error_body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
      return error_body.decode('utf-8', errors='replace')

  async def call(
      self, content: AsyncIterable[content_api.ProcessorPart]
  ) -> AsyncIterable[content_api.ProcessorPart]:
    """Process content through OpenRouter API."""
    messages = []
    async for part in content:
      messages.append(_to_openrouter_message(part))

    if not messages:
      return

    # Prepare request payload
    payload = {
        'model': self._model_name,
        'messages': messages,
        'stream': True,  # Always stream
    }

    # Add configuration parameters
    for key, value in self._config.items():
      if key == 'response_schema' and value is not None:
        # Convert genai_types.SchemaUnion to JSON schema for OpenRouter
        schema_json = _transformers.t_schema(
            _FakeClient(), value
        ).json_schema.model_dump(mode='json', exclude_unset=True)
        payload['response_format'] = {'type': 'json_object', 'schema': schema_json}
      elif key not in ('response_schema', 'tools') and value is not None:
        payload[key] = value

    # Add tools if available
    if self._tools is not None:
      payload['tools'] = self._tools

    # Make streaming request
    async with self._client.stream(
        'POST',
        '/chat/completions',
        json=payload,
    ) as response:
      try:
        response.raise_for_status()
      except httpx.HTTPStatusError as e:
        error_body = await e.response.aread()
        error_detail = self._parse_error_response(error_body)          
        # Handle specific HTTP status codes with appropriate exceptions
        if e.response.status_code == 401:
          raise AuthenticationError(f"Invalid API key: {error_detail}") from e
        elif e.response.status_code == 429:
          retry_after = e.response.headers.get('Retry-After')
          raise RateLimitError(
              f"Rate limit exceeded. Retry after: {retry_after}s" if retry_after 
              else "Rate limit exceeded"
          ) from e
        else:
          raise OpenRouterAPIError(
              f"API request failed ({e.response.status_code}): {error_detail}"
          ) from e

      # Use aiter_lines for easier line processing
      async for line in response.aiter_lines():
        parsed = _parse_sse_line(line)
        if not parsed:
          continue

        if parsed.get('type') == 'done':
          break

        # Extract content from the response
        choices = parsed.get('choices', [])
        if not choices:
          continue

        choice = choices[0]
        delta = choice.get('delta', {})

        # Handle content delta with walrus operator
        if content := delta.get('content'):
          yield content_api.ProcessorPart(
              content,
              role='model',
              metadata=self._build_metadata(parsed),
          )

        # Handle function calls
        if 'function_call' in delta and delta['function_call']:
          func_call = delta['function_call']
          if 'name' in func_call or 'arguments' in func_call:
            # For function calls, we need to accumulate the complete call
            # This is a simplified version - in practice you might want to
            # buffer function calls until complete
            yield content_api.ProcessorPart(
                genai_types.Part.from_function_call(
                    name=func_call.get('name', ''),
                    args=json.loads(func_call.get('arguments', '{}')),
                ),
                role='model',
                metadata=self._build_metadata(parsed),
            )

        # Handle finish reason - use end_of_turn instead of generation_complete
        finish_reason = choice.get('finish_reason')
        if finish_reason:
          yield content_api.ProcessorPart(
              '',
              role='model',
              metadata={
                  **self._build_metadata(parsed),
                  'finish_reason': finish_reason,
                  'end_of_turn': True,
              },
          )

  def _build_metadata(self, response_data: dict[str, Any]) -> dict[str, Any]:
    """Build metadata from OpenRouter response."""
    metadata = {}

    # Add usage information if available
    if 'usage' in response_data:
      metadata['usage'] = response_data['usage']

    # Add model information
    if 'model' in response_data:
      metadata['model'] = response_data['model']

    # Add OpenRouter-specific metadata
    if 'id' in response_data:
      metadata['request_id'] = response_data['id']

    if 'created' in response_data:
      metadata['created'] = response_data['created']

    return metadata

  async def aclose(self):
    """Close the HTTP client."""
    await self._client.aclose()

  async def __aenter__(self):
    """Async context manager entry."""
    return self

  async def __aexit__(self, exc_type, exc_val, exc_tb):
    """Async context manager exit - clean up HTTP client."""
    if hasattr(self, '_client') and self._client:
      await self._client.aclose()


class _FakeClient:
  """A fake genai client to invoke t_schema."""

  def __init__(self):
    self.vertexai = False
