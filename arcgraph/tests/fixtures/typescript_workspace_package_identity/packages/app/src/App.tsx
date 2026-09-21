import React from "react";
import { UI_VERSION } from "@arcgraph/ui";
import { Button } from "@arcgraph/ui/button";
import Missing from "@arcgraph/ui/missing";
import { DuplicateWidget } from "@arcgraph/dupe";

export function App() {
  return <Button label={`${UI_VERSION}:${Missing}:${DuplicateWidget}`} />;
}
