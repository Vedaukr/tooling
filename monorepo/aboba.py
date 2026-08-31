Get-ChildItem -Recurse -Filter project.assets.json | ForEach-Object {
  (Get-Content $_.FullName -Raw | ConvertFrom-Json).libraries.PSObject.Properties.Name
} | Sort-Object -Unique | ForEach-Object {
  $p = $_ -split '/'; [pscustomobject]@{ Id=$p[0]; Version=$p[1] }
} | Group-Object Id | Where-Object Count -gt 1 |
  Select-Object Name, @{n='Versions';e={($_.Group.Version | Sort-Object -Unique) -join ', '}}
