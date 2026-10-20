# Firebase setup (Authentication + Cloud Firestore only)

This project uses Firebase Authentication and Cloud Firestore. It does not use Cloud Storage, storage buckets, or storage rules.

## Local emulator setup

1. Install Java 17+ and the Firebase CLI. No cloud resource is required for the local demo project ID.
2. Copy root `.env.example` to `.env` and `apps/web/.env.example` to `apps/web/.env.local`. Keep the `demo-roofing-local` ID aligned across both files and the emulator command.
3. Start only the configured Auth and Firestore emulators:

   ```sh
   firebase emulators:start --only auth,firestore --project demo-roofing-local --config firebase/firebase.json
   ```

4. In the Firebase Emulator UI at `http://127.0.0.1:4000`, use Authentication to create a test email/password account. The web client connects to Auth at `127.0.0.1:9099`; the API routes Admin calls to Firestore at `127.0.0.1:8080`.
5. With `FIRESTORE_EMULATOR_HOST` configured, Firebase Admin uses anonymous credentials for local Firestore emulator traffic and needs no service-account file. For a real Firebase project, configure ADC or `GOOGLE_APPLICATION_CREDENTIALS` to a backend-only file outside the repository. Emulator hosts must never be set for production.
6. Start the API and web app as described in the README. Verify `GET /health` and `GET /ready` before signing in.

The emulator config uses Firestore rules and indexes from this directory. `firestore.rules` denies all direct client operations; the browser should not access Firestore in the first place. This rules configuration has not been deployed or emulator-tested in the current session.

## Optional Firebase project setup

For a real Firebase project, complete these steps manually:

1. Create/select the Firebase project and add a Web app. Copy its API key, Auth domain, project ID, and app ID into `apps/web/.env.local`. These Web SDK values are public identifiers, not Admin credentials.
2. In Firebase Console, enable **Authentication → Sign-in method → Email/Password**.
3. Create a Cloud Firestore database in the intended region and record the database ID. Use `(default)` unless a named database is configured.
4. Configure backend Application Default Credentials or set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account file outside the repository. Restrict IAM permissions according to deployment needs. Set `FIREBASE_PROJECT_ID`, `FIRESTORE_DATABASE_ID`, exact `CORS_ALLOWED_ORIGINS`, and production secrets outside source control.
5. Review the deny-all rules and the `chunks.data` index exemption, then deploy them **manually** only after you are ready:

   ```sh
   firebase deploy --only firestore:rules,firestore:indexes --config firebase/firebase.json --project YOUR_PROJECT_ID
   ```

No step requires enabling Cloud Storage. Do not deploy rules or create cloud resources as part of ordinary local setup.
