import type { ButtonProps } from '../types';

export function Button(props: ButtonProps) {
  return <button data-tone={props.tone}>{props.label}</button>;
}
