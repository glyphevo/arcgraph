export class ApiClient {
  async fetchStatus(): Promise<string> {
    return 'ok';
  }

  async sendRefresh(): Promise<string> {
    return 'refreshed';
  }
}

export class ApiService {
  private client = new ApiClient();

  async loadViaLocalInstance(): Promise<string> {
    const client = new ApiClient();
    return client.fetchStatus();
  }

  async loadViaThisProperty(): Promise<string> {
    return this.client.fetchStatus();
  }

  async loadViaThisMethod(): Promise<string> {
    return this.normalize(await this.client.fetchStatus());
  }

  private normalize(value: string): string {
    return value.trim();
  }
}
