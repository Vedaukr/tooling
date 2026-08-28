$GitLab = "https://gitlab.example.com"
$Group  = "1234"          # group ID, or URL-encoded path: "my%2Fgroup"
$Token  = "glpat-xxx"
$Dest   = ".\mirrors"

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$headers = @{ "PRIVATE-TOKEN" = $Token }

$projects = @()
$page = 1
while ($true) {
    $uri = "$GitLab/api/v4/groups/$Group/projects?include_subgroups=true&archived=false&per_page=100&page=$page"
    $batch = Invoke-RestMethod -Uri $uri -Headers $headers
    if (-not $batch -or $batch.Count -eq 0) { break }
    $projects += $batch
    $page++
}

Write-Host "Found $($projects.Count) projects"

foreach ($p in $projects) {
    $name   = $p.path_with_namespace -replace '/', '_'
    $target = Join-Path $Dest "$name.git"
    if (Test-Path $target) {
        git -C $target remote update --prune
    } else {
        git clone --mirror $p.ssh_url_to_repo $target
    }
}
