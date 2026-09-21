import { rootValue } from "@arcgraph/conditional";
import { Feature } from "@arcgraph/conditional/feature";
import { envValue } from "#env";
import { browserValue } from "#browser";
import "@arcgraph/conditional/types-only";
import "@arcgraph/conditional/ambiguous";
import "#ambiguous";

export function ConditionalPage(): string {
  return `${rootValue}:${Feature()}:${envValue}:${browserValue}`;
}
