# Git Remote Username Inserter (PowerShell)

This PowerShell script updates the remote URL of a cloned Git repository so that a given username is embedded directly in the HTTPS URL.

Example:  
```
https://example.com/big-company/secret-repo.git
```
becomes  
```
https://employee9999@example.com/big-company/secret-repo.git
```

---

## Features
- Works on any HTTPS Git remote.
- Defaults to the `origin` remote, but you can update any remote name.
- Supports updating **all remotes** at once with `-All`.
- If no `-Path` is given, it uses the **current working directory**.
- Automatically replaces an existing username if one is already present.
- Skips SSH remotes (e.g., `git@github.com:...`).

---

## Installation

1. Save the script as `git-set-username.ps1`.
2. Place it in a directory included in your **PATH**, or add its folder to PATH manually.
   - Example: `C:\Users\<YourUser>\Scripts\`
3. Ensure PowerShell allows running scripts:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```

---

## Usage

### Basic usage (current directory)
```powershell
git-set-username.ps1 -Username employee9999
```

### Specify repository path
```powershell
git-set-username.ps1 -Path "C:\projects\secret-repo" -Username employee9999
```

### Update a different remote
```powershell
git-set-username.ps1 -Username employee9999 -RemoteName upstream
```

### Update all remotes
```powershell
git-set-username.ps1 -Username employee9999 -All
```

---

## Example Output

```
Updated origin:
  Old: https://example.com/big-company/secret-repo.git
  New: https://employee9999@example.com/big-company/secret-repo.git
```

---

## Notes
- Only **HTTPS remotes** are updated. SSH remotes are skipped.
- The script does **not** store your password/token. Git will still prompt you or use a credential manager.
- To remove the username later, run `git remote set-url origin <original-url>` manually.
