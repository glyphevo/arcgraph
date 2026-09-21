import { Button } from "@/components/Button";
import { fetchGreeting } from "@app/services/api";
import { THEME_KEY } from "@config";
import { thing } from "@ambiguous/foo";
import { missing } from "@missing/Ghost";
import { Card } from "components/Card";

export function Home() {
  fetchGreeting();
  return <Button label={THEME_KEY} />;
}

export const HomeCard = () => <Card />;

export const ignored = [thing, missing];
