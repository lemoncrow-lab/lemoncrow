<?php
// Synthetic A/B fixture — PHP dependency injection container.
// NOT for production use. Generated to exercise tree-sitter outline.

declare(strict_types=1);

namespace Container;

use Closure;
use ReflectionClass;
use ReflectionException;
use ReflectionNamedType;
use ReflectionParameter;

class ContainerException extends \RuntimeException {}
class NotFoundException extends ContainerException {}
class CircularDependencyException extends ContainerException {}

interface ContainerInterface
{
    public function get(string $id): mixed;
    public function has(string $id): bool;
    public function bind(string $id, Closure|string $concrete, bool $singleton = false): void;
    public function singleton(string $id, Closure|string $concrete): void;
    public function instance(string $id, object $value): void;
    public function make(string $id, array $params = []): mixed;
}

class Container implements ContainerInterface
{
    private array $bindings = [];
    private array $singletons = [];
    private array $instances = [];
    private array $resolving = [];

    public function bind(string $id, Closure|string $concrete, bool $singleton = false): void
    {
        $this->bindings[$id] = [
            'concrete'  => $concrete,
            'singleton' => $singleton,
        ];
    }

    public function singleton(string $id, Closure|string $concrete): void
    {
        $this->bind($id, $concrete, singleton: true);
    }

    public function instance(string $id, object $value): void
    {
        $this->instances[$id] = $value;
    }

    public function has(string $id): bool
    {
        return isset($this->bindings[$id])
            || isset($this->instances[$id])
            || class_exists($id);
    }

    public function get(string $id): mixed
    {
        return $this->make($id);
    }

    public function make(string $id, array $params = []): mixed
    {
        if (isset($this->instances[$id])) {
            return $this->instances[$id];
        }

        if (in_array($id, $this->resolving, strict: true)) {
            throw new CircularDependencyException(
                sprintf('Circular dependency detected for "%s"', $id)
            );
        }

        $this->resolving[] = $id;
        try {
            $result = $this->resolve($id, $params);
        } finally {
            array_pop($this->resolving);
        }

        if (isset($this->bindings[$id]) && $this->bindings[$id]['singleton']) {
            $this->instances[$id] = $result;
        }
        return $result;
    }

    private function resolve(string $id, array $params): mixed
    {
        if (isset($this->bindings[$id])) {
            $concrete = $this->bindings[$id]['concrete'];
            if ($concrete instanceof Closure) {
                return $concrete($this, $params);
            }
            return $this->make($concrete, $params);
        }

        return $this->build($id, $params);
    }

    private function build(string $id, array $params): object
    {
        try {
            $ref = new ReflectionClass($id);
        } catch (ReflectionException $e) {
            throw new NotFoundException("Cannot resolve \"$id\": " . $e->getMessage(), previous: $e);
        }

        if (!$ref->isInstantiable()) {
            throw new ContainerException("\"$id\" is not instantiable");
        }

        $ctor = $ref->getConstructor();
        if ($ctor === null) {
            return $ref->newInstanceWithoutConstructor();
        }

        $deps = $this->resolveDependencies($ctor->getParameters(), $params);
        return $ref->newInstanceArgs($deps);
    }

    private function resolveDependencies(array $rfParams, array $overrides): array
    {
        $deps = [];
        foreach ($rfParams as $param) {
            /** @var ReflectionParameter $param */
            if (array_key_exists($param->getName(), $overrides)) {
                $deps[] = $overrides[$param->getName()];
                continue;
            }
            $type = $param->getType();
            if ($type instanceof ReflectionNamedType && !$type->isBuiltin()) {
                $deps[] = $this->make($type->getName());
                continue;
            }
            if ($param->isDefaultValueAvailable()) {
                $deps[] = $param->getDefaultValue();
                continue;
            }
            throw new ContainerException(
                sprintf('Cannot resolve parameter "%s"', $param->getName())
            );
        }
        return $deps;
    }

    public function tag(string $tag, string ...$ids): void
    {
        foreach ($ids as $id) {
            $this->bindings["tag:$tag:"][] = $id;
        }
    }

    public function tagged(string $tag): array
    {
        $entries = $this->bindings["tag:$tag:"] ?? [];
        return array_map(fn($id) => $this->make($id), $entries);
    }

    public function extend(string $id, Closure $decorator): void
    {
        $concrete = $this->bindings[$id]['concrete'] ?? $id;
        $this->bindings[$id] = [
            'concrete' => function (Container $c, array $p) use ($concrete, $decorator) {
                $base = ($concrete instanceof Closure) ? $concrete($c, $p) : $c->make($concrete, $p);
                return $decorator($base, $c);
            },
            'singleton' => $this->bindings[$id]['singleton'] ?? false,
        ];
        unset($this->instances[$id]);
    }

    public function flush(): void
    {
        $this->bindings = [];
        $this->instances = [];
        $this->singletons = [];
        $this->resolving = [];
    }

    public function bound(string $id): bool
    {
        return isset($this->bindings[$id]);
    }

    public function resolved(string $id): bool
    {
        return isset($this->instances[$id]);
    }
}

class ServiceProvider
{
    protected Container $container;

    public function __construct(Container $container)
    {
        $this->container = $container;
    }

    public function register(): void {}

    public function boot(): void {}
}

function build_container(array $providers = []): Container
{
    $container = new Container();
    $instances = array_map(fn($cls) => new $cls($container), $providers);
    foreach ($instances as $provider) $provider->register();
    foreach ($instances as $provider) $provider->boot();
    return $container;
}
