# Requisitos de build para o git-server

Este documento registra no `git-server` o contrato de build esperado pelo repositório `da-school`.

## Capacidades necessárias

O servidor já executa múltiplos itens de `build` sequencialmente, tenta os itens seguintes quando um falha e publica uma tag com o SHA curto para cada branch. Para atender ao `da-school`, ele também precisa:

- aceitar `default_branch` na raiz do `repository.yaml`, usando `GIT_DEFAULT_BRANCH` quando a propriedade não existir;
- aceitar `args` em cada item de `build` como um mapa opcional de strings não secretas, com nomes compatíveis com Docker `ARG`;
- encaminhar cada argumento ao BuildKit com `--build-arg`;
- publicar `latest` somente quando a branch recebida for a `default_branch` do repositório;
- manter credenciais e valores de autenticação fora dos argumentos e dos logs.

O parser deve continuar rejeitando nomes OCI inválidos, builds duplicados, campos desconhecidos na raiz ou nos builds, caminhos absolutos, traversal, contextos ou Dockerfiles ausentes e listas de build vazias. Com a ADR-0006, a raiz aceita somente `build`, `tasks`, `mirrors` e `default_branch`.

## Configuração esperada

O `repository.yaml` do `da-school` é a fonte de verdade e declara três imagens com contexto na raiz:

| Nome       | Dockerfile                             | Uso                        |
| ---------- | -------------------------------------- | -------------------------- |
| `api`      | `apps/api/Dockerfile`                  | API e worker               |
| `web`      | `apps/web/Dockerfile`                  | PWA e Nginx                |
| `keycloak` | `infra/keycloak/Dockerfile.production` | Keycloak e keycloak-config |

O ambiente do servidor deve usar:

```env
REGISTRY_ADDRESS=registry.dblsoft.xyz
REGISTRY_INSECURE=false
```

O registry usa TLS e autenticação Basic. Usuário e senha ficam somente no ambiente operacional do servidor.

O hook não consome essas credenciais. Ele envia somente repositório, branch e
SHA ao socket Unix local. O serviço OpenRC `git-build-dispatcher` recebe as
credenciais por `/etc/conf.d/git-build-dispatcher`, arquivo protegido que é
gerado pelo deploy a partir do ambiente do LXC. O gerador, o serviço, o cliente,
o dispatcher e o worker são todos versionados neste repositório.

Para cada push, são esperadas as tags:

- `registry.dblsoft.xyz/da-school/<nome>:<short-sha>` em qualquer branch;
- `registry.dblsoft.xyz/da-school/<nome>:latest` somente em `master`.

## Critérios de aceite no git-server

1. O parser aceita os três builds, seus argumentos e `default_branch: master`.
2. Os comandos BuildKit recebem todos os args declarados; os logs mostram somente os nomes e mascaram os valores.
3. Um push em outra branch publica somente as tags de SHA curto.
4. Um push em `master` publica SHA curto e `latest` para as três imagens.
5. A falha de um item é registrada, os itens seguintes ainda são tentados e o worker termina com falha agregada.
6. Os testes existentes de paths, autenticação e sanitização continuam aprovados.
7. Um job aceito é persistido antes da resposta e recuperado após reinício do
   dispatcher; indisponibilidade ou fila cheia não invalida o push e gera log
   diagnóstico.
8. Registry e BuildKit não ficam disponíveis no ambiente dos processos SSH.
