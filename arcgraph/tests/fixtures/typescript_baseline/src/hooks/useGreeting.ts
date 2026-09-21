import { useEffect, useState } from 'react';
import { GreetingService } from '../services/greeting';

export function useGreeting() {
  const [status, setStatus] = useState('idle');

  useEffect(() => {
    GreetingService.getStatus().then((result) => setStatus(result.status));
  }, []);

  return { status };
}
