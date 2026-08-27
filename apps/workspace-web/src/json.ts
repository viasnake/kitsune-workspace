export type JsonPrimitive = boolean | number | string | null;
// Recursive JSON objects require an index signature; `Record` creates an eager circular alias.
export type JsonObject = { [key: string]: JsonValue };
export type JsonValue = JsonPrimitive | JsonValue[] | JsonObject;

export type JsonSchema = {
  $id?: string;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  title?: string;
  description?: string;
  type?: "array" | "boolean" | "integer" | "null" | "number" | "object" | "string" | string[];
  format?: string;
  enum?: JsonPrimitive[];
  const?: JsonPrimitive;
  default?: JsonValue;
  examples?: JsonValue[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  items?: JsonSchema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  oneOf?: JsonSchema[];
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
};

export type JsonParseResult =
  | { ok: true; value: JsonValue }
  | { ok: false };

const isUnknownRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const isJsonObject = (value: unknown): value is JsonObject =>
  isUnknownRecord(value) &&
  Object.values(value).every(isJsonValue);

export const isJsonValue = (value: unknown): value is JsonValue => {
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return isJsonObject(value);
};

const hasOnlyStrings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");

const hasOnlyJsonPrimitives = (value: unknown): value is JsonPrimitive[] =>
  Array.isArray(value) &&
  value.every(
    (item) =>
      item === null ||
      typeof item === "boolean" ||
      typeof item === "number" ||
      typeof item === "string",
  );

const hasOnlyJsonSchemas = (value: unknown): value is JsonSchema[] =>
  Array.isArray(value) && value.every(isJsonSchema);

const isJsonSchemaMap = (value: unknown): value is Record<string, JsonSchema> =>
  typeof value === "object" &&
  value !== null &&
  !Array.isArray(value) &&
  Object.values(value).every(isJsonSchema);

/** Validate the JSON Schema subset used by the manual-invocation form. */
export const isJsonSchema = (value: unknown): value is JsonSchema => {
  if (!isUnknownRecord(value)) return false;

  const stringFields = ["$id", "$ref", "title", "description", "format", "pattern"];
  if (stringFields.some((field) => value[field] !== undefined && typeof value[field] !== "string")) return false;

  const numberFields = ["minimum", "maximum", "minLength", "maxLength"];
  if (numberFields.some((field) => value[field] !== undefined && typeof value[field] !== "number")) return false;

  if (value.type !== undefined && typeof value.type !== "string" && !hasOnlyStrings(value.type)) return false;
  if (value.enum !== undefined && !hasOnlyJsonPrimitives(value.enum)) return false;
  if (value.const !== undefined && !hasOnlyJsonPrimitives([value.const])) return false;
  if (value.default !== undefined && !isJsonValue(value.default)) return false;
  if (value.examples !== undefined && (!Array.isArray(value.examples) || !value.examples.every(isJsonValue))) return false;
  if (value.properties !== undefined && !isJsonSchemaMap(value.properties)) return false;
  if (value.$defs !== undefined && !isJsonSchemaMap(value.$defs)) return false;
  if (value.required !== undefined && !hasOnlyStrings(value.required)) return false;
  if (
    value.additionalProperties !== undefined &&
    typeof value.additionalProperties !== "boolean" &&
    !isJsonSchema(value.additionalProperties)
  ) return false;
  if (value.items !== undefined && !isJsonSchema(value.items)) return false;
  if (value.oneOf !== undefined && !hasOnlyJsonSchemas(value.oneOf)) return false;
  if (value.anyOf !== undefined && !hasOnlyJsonSchemas(value.anyOf)) return false;
  if (value.allOf !== undefined && !hasOnlyJsonSchemas(value.allOf)) return false;
  return true;
};

export const parseJsonValue = (source: string): JsonParseResult => {
  try {
    const value: unknown = JSON.parse(source);
    return isJsonValue(value) ? { ok: true, value } : { ok: false };
  } catch {
    return { ok: false };
  }
};
