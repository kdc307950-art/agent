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
  const [sidebarOpen, setSidebarOpen] = useState(false)

  return (
    <div className="app-shell">
      <Sidebar mobileOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <Routes>
        <Route path="/" element={<Navigate to="/tickets" replace />} />
        <Route path="/tickets" element={<QueueView onOpenSidebar={() => setSidebarOpen(true)} />} />
        <Route
          path="/tickets/:ticketId"
          element={<QueueView onOpenSidebar={() => setSidebarOpen(true)} />}
        />
        <Route path="/assistant" element={<AssistantView />} />
        <Route path="/assets" element={<AssetsView />} />
        <Route path="/knowledge" element={<KnowledgeView />} />
        <Route path="/it-policies" element={<ItPoliciesView />} />
        <Route path="*" element={<Navigate to="/tickets" replace />} />
      </Routes>
    </div>
  )
}
