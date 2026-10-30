

## ChatGPT ↔ OpenCode : protocole de collaboration

Ce protocole complète les consignes spécifiques du dépôt ; il ne les remplace pas.

### Responsabilités
- **ChatGPT** pilote les missions : inspecte GitHub, fixe le périmètre et les critères de réussite, puis vérifie le commit, le diff et la CI.
- **OpenCode** exécute localement : code, terminal, tests, navigateur et autres outils réellement accessibles.
- **GitHub** conserve les commits poussés, branches, PR, rapports et résultats CI.
- **L'utilisateur** transmet le prompt à OpenCode puis son rapport à ChatGPT. Ne lui délègue pas l'analyse technique ni les validations ordinaires.

### Cycle
1. Vérifier le remote, le dépôt, la branche, le SHA et `git status --short`.
2. Lire les règles locales et l'état pertinent ; éviter les audits répétitifs.
3. Définir une tâche cohérente, un périmètre précis et un résultat mesurable.
4. Préserver les changements locaux préexistants.
5. Travailler sur une branche dédiée pour les changements applicatifs.
6. Exécuter les tests pertinents, inspecter le diff complet et lancer `git diff --check`.
7. Si les vérifications passent, commit ciblé et push de branche. Pas de push direct sur la branche par défaut pour le code applicatif ordinaire.
8. Ne donner une URL de PR qu'après confirmation de sa création.

### Rapport obligatoire
```text
MISSION: <nom court>
REPOSITORY: <owner/repo>
BRANCH: <branche>
COMMIT: <SHA complet ou aucun>
PUSH: oui/non
MODIFICATIONS: <résumé + fichiers essentiels>
TESTS: <commandes exactes + résultats réels>
CI: <URL + statut, ou non déclenchée>
TESTS LOCAUX RÉELS: <faits + résultats>
NON VÉRIFIÉ: <limites, ou aucun>
BLOCAGE: oui/non — <raison>
```

### Boucle de contrôle
À réception du rapport, ChatGPT vérifie directement sur GitHub le commit, le diff et les résultats CI accessibles, puis renvoie le prompt correctif ou la mission suivante. Le rapport seul n'est pas une preuve.

### Efficacité et sécurité
- Modification minimale ; pas de refonte ni dépendance sans justification.
- Après deux échecs d'une même approche, changer de méthode ou signaler le blocage.
- Séparer tests unitaires, intégration, navigateur/runtime réel et production. Une simulation n'est jamais une preuve réelle.
- Ne jamais inventer de résultats, métriques ou fonctionnalités.
- Utiliser uniquement les outils réellement disponibles.
- Pas de secret dans Git, de force-push, réécriture d'historique ou suppression de données.
- Les règles locales de sécurité, produit, paiement, données et production restent prioritaires.
- Pas de migration destructive, fusion sur branche par défaut ou déploiement de production sans autorisation explicite.
- Un commit documentaire ne doit pas provoquer un déploiement applicatif inutile : utiliser `paths-ignore: ['AGENTS.md']` dans les workflows de déploiement concernés, sans désactiver les tests CI utiles.
