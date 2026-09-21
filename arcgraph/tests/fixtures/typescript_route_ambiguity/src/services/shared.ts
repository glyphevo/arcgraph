import { apiClient } from '../api/client';

export class SharedService {
  static async getAmbiguous() {
    return apiClient.get('/shared/status');
  }

  static async getVersioned() {
    return apiClient.get('/api/v2/shared/status');
  }

  static async getPlain() {
    return apiClient.get('/plain/status');
  }

  static async getVideos() {
    return apiClient.get('/videos/list');
  }

  static async getDynamic(version: string) {
    return apiClient.get(`/api/${version}/shared/status`);
  }
}
