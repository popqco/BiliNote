import axios, { AxiosInstance, AxiosResponse } from 'axios';
import toast from 'react-hot-toast'

// 统一响应类型
export interface IResponse<T = any> {
  code: number;
  msg: string;
  data: T;
}

// 允许调用方在 axios 配置里带 suppressToast: true，让拦截器对【预期内的失败】
// 不弹全局红 toast（例如 onboarding 撞名重试、轮询健康检查）。业务代码自己 catch 处理。
declare module 'axios' {
  export interface AxiosRequestConfig {
    suppressToast?: boolean
  }
}

// 模拟一个消息提示函数 (实际项目中会使用UI库的组件，如 Ant Design 的 message 或 Element UI 的 ElMessage)
// This function simulates a message display (in real projects, you'd use a UI library's component)

import { loadWorkerConnection, resolveApiBaseUrl } from '@/utils/workerConnection.ts'

// 创建实例（baseURL 只是初始值：请求拦截器每次按本机配对重算，配对/断开即时生效，无需重载）
// 本机 All-in-One 未配对时走构建期地址，行为与原来一致。
 const request: AxiosInstance = axios.create({
  baseURL: resolveApiBaseUrl(),
  // 30s：后端在繁重任务（抽帧/编码）时接口偶发变慢，10s 会把正常请求误杀成
  // 「请求失败，请检查网络连接」；长耗时接口（如 video_meta 元数据）单独覆盖
  timeout: 30000,
});

// 请求拦截器：每次按本机配对重算 baseURL + 带 token（header 优先，图片场景由调用方拼 query）。
// 本机 All-in-One 未配对时原样放行，行为与原来一致。
request.interceptors.request.use(config => {
  config.baseURL = resolveApiBaseUrl()
  const token = loadWorkerConnection()?.token
  if (token) {
    config.headers = config.headers ?? {}
    ;(config.headers as Record<string, string>)['X-Pairing-Token'] = token
  }
  return config
})

// 响应拦截器
request.interceptors.response.use(
  (response: AxiosResponse<IResponse>) => {
    const res = response.data;
    if (res.code === 0) {
      // 业务成功，可以根据需要显示成功消息，或者不显示（如果操作本身就是可见的）
      // showMessage('success', res.msg || '操作成功'); // 如果需要显示成功消息
      return res.data; // 返回data部分，简化后续业务代码
    } else {
      // 业务错误，统一显示后端返回的错误消息（除非调用方显式 suppressToast）
      if (!response.config?.suppressToast) {
        toast.error(res.msg || '操作失败，请稍后再试');
      }
      return Promise.reject(res); // 拒绝Promise，让业务代码可以捕获并处理
    }
  },
  (error) => {
    const suppress = error?.config?.suppressToast === true
    // 网络/服务器错误
    const res = error?.response?.data as IResponse | undefined;
    if (res) {
      // 未配对 401 不弹 toast：Viewer 未配对时轮询/同步每几秒一次，
      // 弹了就是叠罗汉。业务代码可按需自己处理 code 401。
      const isUnpaired = error?.response?.status === 401
      if (!suppress && !isUnpaired) toast.error(res.msg || '服务器错误，请稍后再试');
      return Promise.reject(res);
    } else {
      // 没有响应数据（如网络中断），显示通用网络错误
      if (!suppress) toast.error('请求失败，请检查网络连接或稍后再试')
      return Promise.reject({
        code: -1,
        msg: '请求失败，请检查网络连接',
        data: null
      } as IResponse);
    }
  }
);

export default request
