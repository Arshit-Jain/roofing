"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { ArrowRight, FileUp, FolderPlus, House, Search, ShieldCheck } from "lucide-react";
import type { User } from "@firebase/auth";
import { useAuth } from "@/components/auth-provider";
import { LoginForm } from "@/components/login-form";
import { ApiError, apiRequest } from "@/lib/api";
import { projectStatuses, type Project, type ProjectStatus } from "@/lib/domain";
import { firebaseConfigured } from "@/lib/firebase";

const statusLabels: Record<ProjectStatus, string> = {
  uploaded: "Add a report",
  processing: "Processing",
  needs_review: "Review measurements",
  ready: "Ready to estimate",
  failed: "Needs attention",
};

function StatusBadge({ status }: { status: ProjectStatus }) {
  const color = status === "ready" ? "green" : status === "failed" ? "red" : status === "needs_review" ? "orange" : "";
  return <span className={`badge ${color}`}>{statusLabels[status]}</span>;
}

function DashboardContent({ user }: { user: User }) {
  const { signOutUser } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [projectsLoadFailed, setProjectsLoadFailed] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | "all">("all");
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let active = true;
    void apiRequest<Project[]>(user, "/api/projects")
      .then((rows) => {
        if (!active) return;
        setProjects(rows);
        setProjectsLoadFailed(false);
        setError("");
      })
      .catch((cause: unknown) => {
        if (active) {
          setProjectsLoadFailed(true);
          setError(cause instanceof ApiError ? cause.message : "Could not load projects.");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [user]);

  async function createProject(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setError("");
    try {
      const project = await apiRequest<Project>(user, "/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: newName.trim() }) });
      setProjects((current) => [project, ...current]);
      setNewName("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create project.");
    } finally {
      setCreating(false);
    }
  }

  const filtered = useMemo(() => projects.filter((project) =>
    (project.name.toLowerCase().includes(search.toLowerCase()) || (project.address ?? "").toLowerCase().includes(search.toLowerCase()))
    && (statusFilter === "all" || project.status === statusFilter)), [projects, search, statusFilter]);
  const needsReview = projects.filter((project) => project.status === "needs_review" || project.status === "failed").length;

  return <div className="shell">
    <header className="topbar">
      <Link href="/" className="brand"><span className="brand-mark"><House size={18} /></span><span>ROOFLINE</span></Link>
      <div className="top-actions"><span>{user.displayName || user.email}</span><button className="button secondary small" onClick={() => void signOutUser()}>Sign out</button></div>
    </header>
    <main className="page">
      <div className="eyebrow">Contractor workspace</div>
      <div className="toolbar">
        <div><h1>Your projects</h1><div className="subcopy">Roof measurements, materials, and estimates together.</div></div>
        <div className="row"><ShieldCheck size={16} color="#0d6a58" /><span className="muted" style={{ fontSize: 12 }}>{projectsLoadFailed ? "Project status unavailable" : needsReview ? `${needsReview} projects need review` : "All projects up to date"}</span></div>
      </div>
      <form className="card panel" onSubmit={createProject} style={{ marginBottom: 24 }}>
        <div className="panel-head"><div><h2>Start an estimate</h2><div className="subcopy">Create a project, then add the Roof Manager measurement PDF.</div></div><FolderPlus size={20} color="#0d6a58" /></div>
        <div className="row" style={{ alignItems: "stretch" }}><input className="input" aria-label="Project name" placeholder="Project name or property address" value={newName} onChange={(event) => setNewName(event.target.value)} required /><button className="button" disabled={creating}><FolderPlus size={16} />{creating ? "Creating…" : "Create project"}</button></div>
      </form>
      {error && <div className="error" role="alert">{error}</div>}
      <div className="toolbar" style={{ margin: "24px 0 13px", alignItems: "center" }}>
        <h2>Recent projects <span className="muted" style={{ fontSize: 13, fontWeight: 500 }}>({projects.length})</span></h2>
        <div className="row" style={{ flexWrap: "wrap", justifyContent: "end" }}>
          <div style={{ position: "relative", width: 230 }}><Search size={15} style={{ position: "absolute", top: 12, left: 11, color: "#849198" }} /><input className="input" style={{ paddingLeft: 34, paddingTop: 9, paddingBottom: 9 }} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search projects" aria-label="Search projects" /></div>
          <select className="input" style={{ width: 170, paddingTop: 9, paddingBottom: 9 }} value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as ProjectStatus | "all")} aria-label="Filter projects by status">
            <option value="all">All statuses</option>
            {projectStatuses.map((status) => <option value={status} key={status}>{statusLabels[status]}</option>)}
          </select>
        </div>
      </div>
      {loading ? <div className="card empty"><p className="muted">Loading your projects…</p></div> : filtered.length ? <div className="grid project-grid">
        {filtered.map((project) => <Link href={`/projects/${project.id}`} key={project.id} className="card project-card">
          <div className="project-card-head"><div><h3>{project.name}</h3><p className="muted">{project.address || "Address not added"}</p></div><StatusBadge status={project.status} /></div>
          <div className="project-stats"><div className="stat"><b>{project.currentMeasurementVersionId ? "Measurements" : "—"}</b><span className="muted">{project.currentMeasurementVersionId ? "Version saved" : "No measurements"}</span></div><div className="stat" style={{ marginLeft: "auto", color: "#0d6a58" }}><b><ArrowRight size={17} /></b><span>Open</span></div></div>
        </Link>)}
      </div> : <div className="card empty"><div className="empty-icon"><FileUp size={21} /></div><h2>{projectsLoadFailed ? "Projects are unavailable" : search ? "No matching projects" : "Your first estimate starts here"}</h2><p className="subcopy">{projectsLoadFailed ? "Reconnect the API, then refresh to load your projects." : search ? "Try another project name." : "Create a project above, then upload your measurement report."}</p></div>}
      <div className="footer-note">Measurement extraction is assistive. Always check source values before preparing a quote.</div>
    </main>
  </div>;
}

export function Dashboard() {
  const { user, loading } = useAuth();
  if (loading) return <div className="card empty"><p className="muted">Checking sign-in…</p></div>;
  if (!user || !firebaseConfigured) return <LoginForm />;
  return <DashboardContent user={user} />;
}
