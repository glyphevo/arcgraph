import { Button } from './components/Button';
import { useGreeting } from './hooks/useGreeting';
import { GreetingService } from './services/greeting';

export function App() {
  const greeting = useGreeting();
  GreetingService.refresh();
  return <Button label={greeting.status} tone="primary" />;
}
