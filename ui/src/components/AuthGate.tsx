import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { SignIn, useAuth } from "@clerk/react";
import { useQueryClient } from "@tanstack/react-query";
import { setTokenGetter } from "../lib/api";
import { Logo } from "./Logo";
import { Loader } from "./Loader";

const LandingPage = lazy(() => import("../pages/LandingPage"));

export const LOGIN_PATH = "/masuk";

function LoginScreen() {
  useEffect(() => {
    document.title = "Masuk - Nirflo";
  }, []);
  return (
    <div className="login">
      <Link to="/" className="login-brand" aria-label="Nirflo, kembali ke beranda">
        <Logo size={36} />
        <span>Nirflo</span>
      </Link>
      <p className="login-tag">Riset dan risiko portofolio saham Indonesia</p>
      <SignIn routing="hash" />
      <Link to="/" className="login-back">Kembali ke beranda</Link>
    </div>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const qc = useQueryClient();
  const { pathname } = useLocation();
  setTokenGetter(() => getToken());
  useEffect(() => {
    if (isLoaded && !isSignedIn) qc.clear();
  }, [isLoaded, isSignedIn, qc]);

  if (!isLoaded) return <Loader fullscreen />;
  if (isSignedIn) return pathname === LOGIN_PATH ? <Navigate to="/" replace /> : <>{children}</>;
  if (pathname === "/") {
    return (
      <Suspense fallback={<Loader fullscreen />}>
        <LandingPage />
      </Suspense>
    );
  }
  return <LoginScreen />;
}