import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import { AuthProvider } from './contexts/AuthContext';

// ── Lazy load pages for code splitting ──
const ReportLibraryPage = lazy(() => import('./pages/ReportLibraryPage'));
const ReportDetailPage = lazy(() => import('./pages/ReportDetailPage'));

function PageFallback() {
  return (
    <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">
      加载中...
    </div>
  );
}

function Nav() {
  const link = ({ isActive }: { isActive: boolean }) =>
    `px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
      isActive ? 'bg-indigo-100 text-indigo-700' : 'text-gray-600 hover:bg-gray-100'
    }`;

  return (
    <nav className="border-b border-gray-200 bg-white sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 flex items-center h-14 gap-2">
        <span className="font-bold text-lg text-gray-900 mr-6">ResearchAgent</span>
        <NavLink to="/library" className={link}>
          研报库
        </NavLink>
        <span className="ml-auto text-xs text-gray-500">采集与问答请通过飞书机器人进行</span>
      </div>
    </nav>
  );
}

function AppLayout() {
  return (
    <div className="min-h-screen bg-gray-50">
      <Nav />
      <main className="max-w-7xl mx-auto px-4 py-6">
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/library" replace />} />
            <Route path="/library" element={<ReportLibraryPage />} />
            <Route path="/library/:id" element={<ReportDetailPage />} />
            <Route path="/tasks" element={<Navigate to="/library" replace />} />
            <Route path="/chat" element={<Navigate to="/library" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/*" element={<AppLayout />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
