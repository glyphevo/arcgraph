export interface Named {
  label: string;
}

export interface ButtonProps extends Named {
  tone: Tone;
}

export interface Greeting {
  status: string;
}

export type Tone = 'primary' | 'secondary';

export enum ViewMode {
  Compact,
  Full,
}

export class BaseWidget {}

export class ButtonModel extends BaseWidget implements ButtonProps {
  label = '';
  tone: Tone = 'primary';
}
