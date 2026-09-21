import { Button } from "@/components/Button";
import { fetchGreeting } from "@app/services/api";
import { API_URL } from "@config";
import { Card } from "components/Card";
import { value as ambiguous } from "@ambiguous/foo";
import { Ghost } from "@missing/Ghost";

export function Home() {
  fetchGreeting();
  console.log(API_URL, ambiguous, Ghost);
  return <Button />;
}

export function HomeCard() {
  return <Card />;
}
