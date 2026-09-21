import { themeName as rootTheme } from "@arcgraph/widgets";
import { Button } from "@arcgraph/widgets/button";
import { themeName } from "@arcgraph/widgets/theme";
import { apiBase } from "#config";
import { add } from "#lib/math";
import { missing } from "@arcgraph/widgets/missing";
import { ambiguousValue } from "@arcgraph/widgets/ambiguous";
import { internalMissing } from "#missing";

export function Dashboard() {
  const total = add(1, 2);
  return <Button />;
}

export const diagnostics = [
  themeName,
  rootTheme,
  apiBase,
  missing,
  ambiguousValue,
  internalMissing,
];
