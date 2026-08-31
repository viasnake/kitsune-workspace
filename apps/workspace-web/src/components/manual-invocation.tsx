import { type ComponentProps, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Braces, Play, ShieldAlert } from "lucide-react";
import {
  workspaceApi,
  type Handler,
} from "../api/generated";
import { isJsonSchema, parseJsonValue, type JsonSchema, type JsonValue } from "../json";
import { queryKeys } from "../query-keys";
import { stringifyJson } from "../utils";
import { ErrorState, LoadingButton, PermissionHint } from "./ui";

type ObjectValue = Record<string, JsonValue>;
type GeneratedSchemaType = "array" | "boolean" | "integer" | "number" | "object" | "string";

const generatedSchemaTypes: readonly GeneratedSchemaType[] = [
  "array",
  "boolean",
  "integer",
  "number",
  "object",
  "string",
];

const isGeneratedSchemaType = (value: string): value is GeneratedSchemaType =>
  (generatedSchemaTypes as readonly string[]).includes(value);

const typeIncludes = (schema: JsonSchema, type: string): boolean =>
  Array.isArray(schema.type) ? schema.type.includes(type) : schema.type === type;

const resolveLocalReference = (reference: string, root: JsonSchema): JsonSchema | null => {
  if (!reference.startsWith("#/")) return null;
  let current: unknown = root;
  for (const encodedToken of reference.slice(2).split("/")) {
    const token = encodedToken.replaceAll("~1", "/").replaceAll("~0", "~");
    if (
      typeof current !== "object" ||
      current === null ||
      Array.isArray(current) ||
      !Object.hasOwn(current, token)
    ) {
      return null;
    }
    current = (current as Record<string, unknown>)[token];
  }
  return isJsonSchema(current) ? current : null;
};

const isNullSchema = (schema: JsonSchema): boolean =>
  schema.type === "null" ||
  (Array.isArray(schema.type) && schema.type.length === 1 && schema.type[0] === "null");

const normalizeGeneratedField = (
  schema: JsonSchema,
  root: JsonSchema,
  activeReferences: ReadonlySet<string> = new Set(),
): JsonSchema | null => {
  let candidate = { ...schema };

  if (candidate.$ref !== undefined) {
    if (activeReferences.has(candidate.$ref)) return null;
    const target = resolveLocalReference(candidate.$ref, root);
    if (target === null) return null;
    const reference = candidate.$ref;
    delete candidate.$ref;
    candidate = { ...target, ...candidate };
    return normalizeGeneratedField(candidate, root, new Set([...activeReferences, reference]));
  }

  if (candidate.anyOf !== undefined) {
    const nonNull = candidate.anyOf.filter((branch) => !isNullSchema(branch));
    const nullCount = candidate.anyOf.length - nonNull.length;
    if (candidate.anyOf.length !== 2 || nonNull.length !== 1 || nullCount !== 1) return null;
    delete candidate.anyOf;
    candidate = { ...nonNull[0], ...candidate };
    return normalizeGeneratedField(candidate, root, activeReferences);
  }

  if (Array.isArray(candidate.type)) {
    const nonNull = candidate.type.filter((type) => type !== "null");
    if (nonNull.length !== 1 || candidate.type.length > 2) return null;
    const normalizedType = nonNull[0];
    if (normalizedType === undefined || !isGeneratedSchemaType(normalizedType)) return null;
    candidate.type = normalizedType;
  }

  delete candidate.$defs;
  if (candidate.properties !== undefined) {
    const properties: Record<string, JsonSchema> = {};
    for (const [name, property] of Object.entries(candidate.properties)) {
      const normalized = normalizeGeneratedField(property, root, activeReferences);
      if (normalized === null) return null;
      properties[name] = normalized;
    }
    candidate.properties = properties;
  }
  if (candidate.items !== undefined) {
    const items = normalizeGeneratedField(candidate.items, root, activeReferences);
    if (items === null) return null;
    candidate.items = items;
  }
  if (typeof candidate.additionalProperties === "object") {
    const additionalProperties = normalizeGeneratedField(
      candidate.additionalProperties,
      root,
      activeReferences,
    );
    if (additionalProperties === null) return null;
    candidate.additionalProperties = additionalProperties;
  }
  return candidate;
};

const supportsGeneratedField = (schema: JsonSchema): boolean => {
  if (
    schema.$ref !== undefined ||
    schema.$defs !== undefined ||
    schema.const !== undefined ||
    schema.oneOf !== undefined ||
    schema.anyOf !== undefined ||
    schema.allOf !== undefined ||
    Array.isArray(schema.type)
  ) {
    return false;
  }
  if (
    schema.enum !== undefined &&
    !schema.enum.every((value) => typeof value === "string" || typeof value === "number")
  ) {
    return false;
  }
  if (schema.properties !== undefined && !Object.values(schema.properties).every(supportsGeneratedField)) {
    return false;
  }
  if (schema.items !== undefined && !supportsGeneratedField(schema.items)) return false;
  if (
    typeof schema.additionalProperties === "object" &&
    !supportsGeneratedField(schema.additionalProperties)
  ) {
    return false;
  }
  return (
    schema.enum !== undefined ||
    generatedSchemaTypes.some((type) => typeIncludes(schema, type))
  );
};

const supportsGeneratedForm = (
  schema: JsonSchema | null,
): schema is JsonSchema & { properties: Record<string, JsonSchema> } =>
  schema !== null &&
  typeIncludes(schema, "object") &&
  schema.properties !== undefined &&
  supportsGeneratedField(schema);

const generatedFormSchema = (
  reportedSchema: unknown,
): (JsonSchema & { properties: Record<string, JsonSchema> }) | null => {
  if (!isJsonSchema(reportedSchema)) return null;
  const normalized = normalizeGeneratedField(reportedSchema, reportedSchema);
  return supportsGeneratedForm(normalized) ? normalized : null;
};

const initialObject = (schema: JsonSchema): ObjectValue => {
  const result: ObjectValue = {};
  for (const [name, property] of Object.entries(schema.properties ?? {})) {
    if (property.default !== undefined) result[name] = property.default;
    else if (typeIncludes(property, "boolean")) result[name] = false;
    else if (typeIncludes(property, "array")) result[name] = [];
    else if (typeIncludes(property, "object")) result[name] = {};
  }
  return result;
};

function SchemaField({
  name,
  schema,
  value,
  required,
  onChange,
}: {
  name: string;
  schema: JsonSchema;
  value: JsonValue | undefined;
  required: boolean;
  onChange: (value: JsonValue) => void;
}) {
  const label = schema.title ?? name;
  const inputId = `invoke-${name}`;
  const common = { id: inputId, name, required, "aria-describedby": schema.description === undefined ? undefined : `${inputId}-help` };
  const serializedValue = stringifyJson(
    value ?? (typeIncludes(schema, "array") ? [] : {}),
  );
  const [jsonDraft, setJsonDraft] = useState(serializedValue);

  if (schema.enum !== undefined) {
    return (
      <label className="form-field" htmlFor={inputId}>
        <span>
          {label} {required && <b aria-label="必須">*</b>}
        </span>
        <select
          {...common}
          value={typeof value === "string" || typeof value === "number" ? String(value) : ""}
          onChange={(event) => {
            const selected = schema.enum?.find((option) => String(option) === event.target.value);
            onChange(selected ?? event.target.value);
          }}
        >
          <option value="" disabled={required}>
            選択してください
          </option>
          {schema.enum.map((option) => (
            <option value={String(option)} key={String(option)}>
              {String(option)}
            </option>
          ))}
        </select>
        {schema.description !== undefined && <small id={`${inputId}-help`}>{schema.description}</small>}
      </label>
    );
  }

  if (typeIncludes(schema, "boolean")) {
    return (
      <label className="checkbox-field" htmlFor={inputId}>
        <input {...common} type="checkbox" checked={value === true} onChange={(event) => onChange(event.target.checked)} />
        <span>
          <strong>{label}</strong>
          {schema.description !== undefined && <small id={`${inputId}-help`}>{schema.description}</small>}
        </span>
      </label>
    );
  }

  if (typeIncludes(schema, "integer") || typeIncludes(schema, "number")) {
    return (
      <label className="form-field" htmlFor={inputId}>
        <span>
          {label} {required && <b aria-label="必須">*</b>}
        </span>
        <input
          {...common}
          type="number"
          step={typeIncludes(schema, "integer") ? 1 : "any"}
          min={schema.minimum}
          max={schema.maximum}
          value={typeof value === "number" ? value : ""}
          onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))}
        />
        {schema.description !== undefined && <small id={`${inputId}-help`}>{schema.description}</small>}
      </label>
    );
  }

  if (typeIncludes(schema, "array") || typeIncludes(schema, "object")) {
    return (
      <label className="form-field" htmlFor={inputId}>
        <span>
          {label} {required && <b aria-label="必須">*</b>}
          <em>JSON</em>
        </span>
        <textarea
          {...common}
          className="code-input compact-code-input"
          rows={5}
          value={jsonDraft}
          onChange={(event) => {
            setJsonDraft(event.target.value);
            const parsed = parseJsonValue(event.target.value);
            if (parsed.ok) {
              event.target.setCustomValidity("");
              onChange(parsed.value);
            } else {
              event.target.setCustomValidity("有効な JSON を入力してください");
            }
          }}
          onBlur={(event) => event.currentTarget.reportValidity()}
        />
        {schema.description !== undefined && <small id={`${inputId}-help`}>{schema.description}</small>}
      </label>
    );
  }

  return (
    <label className="form-field" htmlFor={inputId}>
      <span>
        {label} {required && <b aria-label="必須">*</b>}
      </span>
      {schema.format === "textarea" ? (
        <textarea
          {...common}
          rows={4}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          {...common}
          type={schema.format === "date-time" ? "datetime-local" : "text"}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          pattern={schema.pattern}
          value={typeof value === "string" ? value : ""}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {schema.description !== undefined && <small id={`${inputId}-help`}>{schema.description}</small>}
    </label>
  );
}

export function ManualInvocation({
  agentId,
  handlers,
  csrfToken,
  allowed,
}: {
  agentId: string;
  handlers: Handler[];
  csrfToken: string;
  allowed: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [handlerName, setHandlerName] = useState(handlers[0]?.name ?? "");
  const handler = useMemo(() => handlers.find((candidate) => candidate.name === handlerName) ?? handlers[0], [handlerName, handlers]);
  const reportedInputSchema: unknown = handler?.input_schema;
  const generatedSchema = useMemo(
    () => generatedFormSchema(reportedInputSchema),
    [reportedInputSchema],
  );
  const [objectInput, setObjectInput] = useState<ObjectValue>(() => (generatedSchema === null ? {} : initialObject(generatedSchema)));
  const [jsonInput, setJsonInput] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [timeout, setTimeoutValue] = useState<number | "">(handler?.default_timeout_seconds ?? "");

  const selectHandler = (name: string) => {
    const nextHandler = handlers.find((candidate) => candidate.name === name);
    const reportedNextSchema: unknown = nextHandler?.input_schema;
    const nextSchema = generatedFormSchema(reportedNextSchema);
    setHandlerName(name);
    setObjectInput(nextSchema === null ? {} : initialObject(nextSchema));
    setJsonInput("{}");
    setJsonError(null);
    setTimeoutValue(nextHandler?.default_timeout_seconds ?? "");
  };

  const mutation = useMutation({
    mutationFn: (input: JsonValue) =>
      workspaceApi.createRun(
        agentId,
        {
          handler: handler?.name ?? handlerName,
          input,
          ...(timeout === "" ? {} : { timeout_seconds: timeout }),
        },
        csrfToken,
      ),
    onSuccess: async (run) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
        queryClient.invalidateQueries({ queryKey: queryKeys.agent(agentId) }),
      ]);
      await navigate({ to: "/runs/$runId", params: { runId: run.run_id } });
    },
  });

  if (handlers.length === 0) {
    return <ErrorState error={new Error("起動中の Agent から Handler が報告されていません。")} compact />;
  }

  if (!allowed) {
    return (
      <div className="permission-panel">
        <ShieldAlert aria-hidden="true" size={22} />
        <div>
          <strong>手動実行には operator 権限が必要です</strong>
          <p>Handler の Schema は確認できますが、このセッションから Run は作成できません。</p>
        </div>
        <PermissionHint>閲覧のみ</PermissionHint>
      </div>
    );
  }

  const submit: NonNullable<ComponentProps<"form">["onSubmit"]> = (event) => {
    event.preventDefault();
    if (handler === undefined) return;
    if (generatedSchema !== null) {
      mutation.mutate(objectInput);
      return;
    }
    const parsed = parseJsonValue(jsonInput);
    if (parsed.ok) {
      setJsonError(null);
      mutation.mutate(parsed.value);
    } else {
      setJsonError("有効な JSON を入力してください。");
    }
  };

  return (
    <form className="invocation-form" onSubmit={submit}>
      <div className="invocation-form-heading">
        <div>
          <h3>Manual Invocation</h3>
          <p>選択した Handler の入力 Schema に従って on_demand Run を作成します。</p>
        </div>
        <label className="select-field handler-select">
          <span>Handler</span>
          <select value={handler?.name ?? ""} onChange={(event) => selectHandler(event.target.value)}>
            {handlers.map((candidate) => (
              <option key={candidate.name} value={candidate.name}>
                {candidate.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {handler?.description !== null && handler?.description !== undefined && <p className="handler-description">{handler.description}</p>}

      {generatedSchema !== null ? (
        <div className="schema-form-grid">
          {Object.entries(generatedSchema.properties).map(([name, schema]) => (
            <SchemaField
              key={`${handler?.name ?? handlerName}:${name}`}
              name={name}
              schema={schema}
              value={objectInput[name]}
              required={generatedSchema.required?.includes(name) ?? false}
              onChange={(value) => setObjectInput((current) => ({ ...current, [name]: value }))}
            />
          ))}
        </div>
      ) : (
        <label className="form-field" htmlFor="manual-json-input">
          <span>
            <Braces aria-hidden="true" size={15} />
            Input JSON
          </span>
          <textarea
            id="manual-json-input"
            className="code-input"
            rows={12}
            value={jsonInput}
            onChange={(event) => {
              setJsonInput(event.target.value);
              setJsonError(null);
            }}
            aria-invalid={jsonError !== null}
            aria-describedby={jsonError === null ? undefined : "manual-json-error"}
          />
          {jsonError !== null && (
            <small id="manual-json-error" className="field-error" role="alert">
              {jsonError}
            </small>
          )}
        </label>
      )}

      <div className="invocation-footer">
        <label className="form-field timeout-field" htmlFor="manual-timeout">
          <span>Timeout（秒）</span>
          <input
            id="manual-timeout"
            type="number"
            min={1}
            value={timeout}
            placeholder="Manifest の既定値"
            onChange={(event) => setTimeoutValue(event.target.value === "" ? "" : Number(event.target.value))}
          />
        </label>
        <LoadingButton className="button button-primary" type="submit" loading={mutation.isPending}>
          <Play aria-hidden="true" size={16} />
          Run を開始
        </LoadingButton>
      </div>
      {mutation.error !== null && <ErrorState error={mutation.error} compact />}
    </form>
  );
}
