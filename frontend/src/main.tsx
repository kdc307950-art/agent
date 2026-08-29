/**
 * 应用入口文件（main.tsx）。
 *
 * 职责：
 * - 将整个 React 应用挂载到 index.html 的 #root 节点，是前端启动的入口代码。
 * - StrictMode 开启开发期严格模式：组件会额外渲染、副作用会重复执行，用于尽早暴露潜在问题（生产构建无影响）。
 * - BrowserRouter 提供基于 History API 的路由上下文，使 App 内部的 <Routes>/<Route> 能正常工作。
 *
 * 与后端 API 的关系：本文件不直接请求后端；
 * 数据获取由各视图组件（views/ 目录）通过 src/api 的统一封装层完成。
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App'

// 将应用渲染进 #root 挂载点；! 断言该节点一定存在（由 index.html 提供）
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
