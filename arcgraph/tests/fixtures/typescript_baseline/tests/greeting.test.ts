import { GreetingService } from '../src/services/greeting';

export async function loadStatusSpec() {
  return GreetingService.getStatus();
}
