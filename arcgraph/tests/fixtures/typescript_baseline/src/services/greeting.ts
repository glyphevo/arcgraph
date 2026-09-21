import axios from 'axios';
import { apiClient } from '../api/client';
import type { Greeting } from '../types';

export class GreetingService {
  static async getStatus(): Promise<Greeting> {
    return apiClient.get('/greeting/status');
  }

  static async refresh(): Promise<Greeting> {
    return apiClient.post('/greeting/refresh');
  }

  static async getViaAxios(): Promise<Greeting> {
    return axios.get('/greeting/status');
  }

  static async getDynamic(name: string): Promise<string> {
    return apiClient.get(`/greeting/${name}`);
  }
}
