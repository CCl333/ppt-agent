import { useEffect, useState } from 'react';
import Home from './components/Home';
import ProjectStart from './components/ProjectStart';
import Editor from './components/Editor';
import { getProject, type ProjectSummary } from './lib/ppt-api';

export default function App() {
  const [view, setView] = useState<'home' | 'workspace'>('home');
  const [activeProject, setActiveProject] = useState<ProjectSummary | null>(null);

  useEffect(() => {
    const match = window.location.hash.match(/^#\/p\/([^/]+)/);
    if (!match) {
      return;
    }
    let cancelled = false;
    getProject(match[1])
      .then((project) => {
        if (!cancelled) {
          setActiveProject(project);
        }
      })
      .catch(() => {
        if (!cancelled) {
          window.location.hash = '';
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onHashChange = () => {
      const match = window.location.hash.match(/^#\/p\/([^/]+)/);
      if (!match) {
        setActiveProject(null);
        setView('home');
        return;
      }
      if (activeProject?.project_id === match[1]) {
        return;
      }
      void getProject(match[1])
        .then(setActiveProject)
        .catch(() => {
          window.location.hash = '';
        });
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [activeProject?.project_id]);

  useEffect(() => {
    if (!activeProject) {
      return;
    }
    if (view === 'home') {
      setView('workspace');
    }
  }, [activeProject, view]);

  const openProject = (project: ProjectSummary) => {
    window.location.hash = `#/p/${project.project_id}`;
    setActiveProject(project);
  };

  const closeProject = () => {
    window.location.hash = '';
    setActiveProject(null);
    setView('home');
  };

  return (
    <div className="min-h-screen bg-[#f8f9fa] text-slate-800 font-sans">
      {view === 'home' && <Home onStart={openProject} />}
      {view === 'workspace' && activeProject && activeProject.current_stage === 'init' && (
        <ProjectStart
          project={activeProject}
          onBack={closeProject}
          onProjectUpdated={setActiveProject}
        />
      )}
      {view === 'workspace' && activeProject && activeProject.current_stage !== 'init' && (
        <Editor
          project={activeProject}
          onBack={closeProject}
          onProjectUpdated={setActiveProject}
        />
      )}
    </div>
  );
}
