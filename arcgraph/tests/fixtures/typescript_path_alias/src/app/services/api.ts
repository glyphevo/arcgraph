export function fetchGreeting() {
  return fetch("/api/v1/greeting/status");
}
