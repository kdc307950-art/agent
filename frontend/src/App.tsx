/**
 * 应用根组件（App.tsx）。
 *
 * 职责：
 * - 组装整体页面骨架：左侧 Sidebar 导航 + 右侧路由内容区。
 * - 定义全站路由表：根路径 / 与未知路径一律重定向到 /tickets；其余路径映射到对应视图。
 * - 管理移动端侧边栏开关状态 sidebarOpen，并通过 props 回调让各视图（如 QueueView 顶栏菜单按钮）能打开侧栏。
 *
 * 与后端 API 的对应关系：本组件不直接调用 API；
 * 各 <Route> 挂载的视图（QueueView / AssistantView / AssetsView / KnowledgeView / ItPoliciesView）
 * 通过 src/api 封装层与 FastAPI 后端通信。
 */
import { useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import './App.css'
import Sidebar from './components/Sidebar'
import QueueView from './views/QueueView'
import AssistantView from './views/AssistantView'
import AssetsView from './views/AssetsView'
import KnowledgeView from './views/KnowledgeView'
import ItPoliciesView from './views/ItPoliciesView'

export default function App() {
  // sidebarOpen：移动端侧栏是否展开（桌面端侧栏常驻，此状态仅在窄屏生效）
  const [sidebarOpen, setSidebarOpen] = useState(false)

  return (
    <div className="app-shell">
      {/* 侧栏（含 Dev 演示令牌输入条）：mobileOpen 控制展开；onClose 由侧栏内部触发收起 */}
      <Sidebar mobileOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <Routes>
        {/* 根路径与未知路径都重定向到工单队列，保证任何 URL 都有可用页面 */}
        <Route path="/" element={<Navigate to="/tickets" replace />} />
        {/* 工单列表页 */}
        <Route path="/tickets" element={<QueueView onOpenSidebar={() => setSidebarOpen(true)} />} />
        {/* 工单详情页：列表与详情共用 QueueView，靠 URL 参数 :ticketId 区分展示模式 */}
        <Route
          path="/tickets/:ticketId"
          element={<QueueView onOpenSidebar={() => setSidebarOpen(true)} />}
        />
        <Route path="/assistant" element={<AssistantView />} />
        <Route path="/assets" element={<AssetsView />} />
        <Route path="/knowledge" element={<KnowledgeView />} />
        <Route path="/it-policies" element={<ItPoliciesView />} />
        {/* 兜底：未匹配的任意路径回到工单队列 */}
        <Route path="*" element={<Navigate to="/tickets" replace />} />
      </Routes>
    </div>
  )
}
