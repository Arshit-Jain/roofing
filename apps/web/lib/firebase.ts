import { getApp, getApps, initializeApp } from "@firebase/app";
import { connectAuthEmulator, getAuth } from "@firebase/auth";

const firebaseConfig = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY ?? "",
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN ?? "",
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID ?? "",
  appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID ?? "",
};

export const firebaseConfigured = Object.values(firebaseConfig).every(Boolean);
export const firebaseApp = firebaseConfigured
  ? getApps().length ? getApp() : initializeApp(firebaseConfig)
  : null;
export const auth = firebaseApp ? getAuth(firebaseApp) : null;

export const authEmulatorHost = process.env.NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_HOST;
const emulatorState = globalThis as typeof globalThis & { __rooflineAuthEmulatorConnected?: boolean };
if (auth && authEmulatorHost && typeof window !== "undefined" && !emulatorState.__rooflineAuthEmulatorConnected) {
  const [host, portText] = authEmulatorHost.split(":");
  const port = Number(portText || "9099");
  // The browser client only talks to the Auth emulator. The API owns Firestore access.
  connectAuthEmulator(auth, `http://${host}:${port}`, { disableWarnings: true });
  emulatorState.__rooflineAuthEmulatorConnected = true;
}
