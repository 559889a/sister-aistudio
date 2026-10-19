"""HTTP request schemas."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field, field_validator


class GeminiInlineData(BaseModel):
    mimeType: str = Field(validation_alias=AliasChoices("mimeType", "mime_type"))
    data: str


class GeminiFileData(BaseModel):
    mimeType: str | None = Field(
        default=None, validation_alias=AliasChoices("mimeType", "mime_type")
    )
    fileUri: str = Field(validation_alias=AliasChoices("fileUri", "file_uri"))


class GeminiFunctionCall(BaseModel):
    name: str
    args: dict[str, object] | list[object] | object | None = Field(
        default=None, validation_alias=AliasChoices("args", "arguments")
    )
    id: str | None = None


class GeminiFunctionResponse(BaseModel):
    name: str
    response: dict[str, object] | list[object] | object | None = Field(
        default=None,
        validation_alias=AliasChoices("response", "content", "output"),
    )
    id: str | None = None
    parts: list[GeminiPart] | None = None


class GeminiExecutableCode(BaseModel):
    code: str
    language: str = Field(default="PYTHON")
    id: str | None = None


class GeminiCodeExecutionResult(BaseModel):
    outcome: str
    output: str
    id: str | None = None


class GeminiPart(BaseModel):
    text: str | None = None
    inlineData: GeminiInlineData | None = Field(
        default=None, validation_alias=AliasChoices("inlineData", "inline_data")
    )
    fileData: GeminiFileData | None = Field(
        default=None, validation_alias=AliasChoices("fileData", "file_data")
    )
    thought: bool | None = None
    thoughtSignature: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thoughtSignature", "thought_signature"),
    )
    functionCall: GeminiFunctionCall | None = Field(
        default=None, validation_alias=AliasChoices("functionCall", "function_call")
    )
    functionResponse: GeminiFunctionResponse | None = Field(
        default=None,
        validation_alias=AliasChoices("functionResponse", "function_response"),
    )
    executableCode: GeminiExecutableCode | None = Field(
        default=None,
        validation_alias=AliasChoices("executableCode", "executable_code"),
    )
    codeExecutionResult: GeminiCodeExecutionResult | None = Field(
        default=None,
        validation_alias=AliasChoices("codeExecutionResult", "code_execution_result"),
    )


GeminiFunctionResponse.model_rebuild()


class GeminiContent(BaseModel):
    role: str | None = None
    parts: list[GeminiPart]


class GeminiTool(BaseModel):
    codeExecution: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("codeExecution", "code_execution")
    )
    googleSearch: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("googleSearch", "google_search")
    )
    googleSearchRetrieval: dict[str, object] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "googleSearchRetrieval", "google_search_retrieval"
        ),
    )
    googleMaps: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("googleMaps", "google_maps")
    )
    urlContext: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("urlContext", "url_context")
    )
    functionDeclarations: list[dict[str, object]] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "functionDeclarations",
            "function_declarations",
            "functionDeclaration",
            "function_declaration",
        ),
    )

    @field_validator("functionDeclarations", mode="before")
    @classmethod
    def _coerce_function_declarations(cls, v: object) -> object:
        if isinstance(v, dict):
            return [v]
        return v


class GeminiGenerationConfig(BaseModel):
    stopSequences: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("stopSequences", "stop_sequences")
    )
    temperature: float | None = None
    topP: float | None = Field(
        default=None, validation_alias=AliasChoices("topP", "top_p")
    )
    topK: int | None = Field(
        default=None, validation_alias=AliasChoices("topK", "top_k")
    )
    maxOutputTokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxOutputTokens", "max_output_tokens"),
    )
    responseModalities: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("responseModalities", "response_modalities"),
    )
    responseMimeType: str | None = Field(
        default=None,
        validation_alias=AliasChoices("responseMimeType", "response_mime_type"),
    )
    responseSchema: list[object] | dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("responseSchema", "response_schema")
    )
    presencePenalty: float | None = Field(
        default=None,
        validation_alias=AliasChoices("presencePenalty", "presence_penalty"),
    )
    frequencyPenalty: float | None = Field(
        default=None,
        validation_alias=AliasChoices("frequencyPenalty", "frequency_penalty"),
    )
    responseLogprobs: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("responseLogprobs", "response_logprobs"),
    )
    logprobs: int | None = None
    mediaResolution: list[object] | int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("mediaResolution", "media_resolution"),
    )
    thinkingConfig: list[object] | dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("thinkingConfig", "thinking_config")
    )
    imageConfig: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("imageConfig", "image_config")
    )


class GeminiSafetySetting(BaseModel):
    category: str
    threshold: str


# ── OpenAI 兼容端口的请求模型（POST /v1/chat/completions） ──
#
# 只做「够用的校验」：OpenAI 客户端会带一堆扩展字段（response_format / seed /
# user / logprobs …），没声明的字段一律忽略，不能因为多一个键就 422。


class OpenAIFunctionCall(BaseModel):
    name: str
    arguments: str | dict[str, object] | None = None


class OpenAIToolCall(BaseModel):
    id: str | None = None
    type: str | None = "function"
    index: int | None = None
    function: OpenAIFunctionCall
    # 主项目的扩展字段：Google 的思考签名挂在 tool_call 上往返
    extra_content: dict[str, object] | None = None
    thought_signature: str | None = None
    thoughtSignature: str | None = None


class OpenAIChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, object]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[OpenAIToolCall] | None = None
    reasoning_content: str | None = None
    extra_content: dict[str, object] | None = None


class OpenAIFunctionDefinition(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, object] | None = None
    strict: bool | None = None


class OpenAITool(BaseModel):
    type: str | None = "function"
    function: OpenAIFunctionDefinition | None = None


class OpenAIStreamOptions(BaseModel):
    include_usage: bool = False


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: list[OpenAIChatMessage]
    stream: bool = False
    stream_options: OpenAIStreamOptions | None = None
    temperature: float | None = None
    top_p: float | None = None
    # OpenAI 规范里没有 top_k，但不少客户端（SillyTavern 等）会带，主项目也认
    top_k: int | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    tools: list[OpenAITool] | None = None
    tool_choice: str | dict[str, object] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    n: int | None = None
    reasoning_effort: str | None = None
    thinking: dict[str, object] | None = None
    response_format: dict[str, object] | None = None
    safety_settings: list[dict[str, object]] | None = None
    seed: int | None = None
    user: str | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None


class GeminiGenerateContentRequest(BaseModel):
    contents: list[GeminiContent]
    systemInstruction: GeminiContent | None = Field(
        default=None,
        validation_alias=AliasChoices("systemInstruction", "system_instruction"),
    )
    tools: list[GeminiTool] | None = None
    generationConfig: GeminiGenerationConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("generationConfig", "generation_config"),
    )
    safetySettings: list[GeminiSafetySetting] | None = Field(
        default=None, validation_alias=AliasChoices("safetySettings", "safety_settings")
    )
    toolConfig: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("toolConfig", "tool_config")
    )
