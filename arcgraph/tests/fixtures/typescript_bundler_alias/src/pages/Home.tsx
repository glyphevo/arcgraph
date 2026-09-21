import { fetchGreeting } from "@app/services/api";
import { ambiguousValue } from "@ambiguous/foo";
import { Button } from "@/components/Button";
import { configValue } from "@config";
import { missing } from "@missing/Ghost";
import { dynamicValue } from "@dynamic/foo";

export function Home() {
  fetchGreeting();
  return <Button />;
}

export const diagnostics = [
  configValue,
  missing,
  ambiguousValue,
  dynamicValue,
];
