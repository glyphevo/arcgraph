import DefaultClient from './domain/defaultClient';
import { ApiClient, ApiService } from './domain/service';
import * as ServiceModule from './domain/service';
import type { RemoteContract } from './domain/contracts';
import type { WirePayload } from './domain/typeOnly';

declare const require: (path: string) => any;

const { legacyHelper } = require('./domain/legacy');

export type { WirePayload } from './domain/typeOnly';
export type { RemoteContract } from './domain/contracts';

export async function runPhaseTwo(raw: WirePayload, remote?: RemoteContract) {
  const service = new ApiService();
  const direct = await service.loadViaLocalInstance();
  const client = new ApiClient();
  const status = await client.fetchStatus();
  const defaultClient = new DefaultClient();
  const loaded = defaultClient.load();
  const namespaced = new ServiceModule.ApiClient();
  const namespaceStatus = await namespaced.fetchStatus();
  const legacy = legacyHelper(raw.value);
  const viaFetch = await fetch('/api/v1/greeting/status');
  const refreshed = await fetch('/api/v1/greeting/refresh', { method: 'POST' });

  return {
    direct,
    status,
    loaded,
    namespaceStatus,
    legacy,
    viaFetch,
    refreshed,
    remoteId: remote?.id,
  };
}

export async function dynamicRoute(name: string) {
  return fetch(`/api/v1/greeting/${name}`);
}
