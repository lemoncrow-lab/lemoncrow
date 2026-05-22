// Synthetic A/B fixture — Scala functional config loader with validation.
// NOT for production use. Generated to exercise tree-sitter outline.

package config

import scala.util.{Failure, Success, Try}
import scala.collection.immutable.Map

// ---------- Error types ----------

sealed trait ConfigError
case class MissingKey(key: String) extends ConfigError
case class ParseError(key: String, raw: String, hint: String) extends ConfigError
case class ValidationError(key: String, message: String) extends ConfigError
case class MultiError(errors: List[ConfigError]) extends ConfigError

object ConfigError {
  def render(e: ConfigError): String = e match {
    case MissingKey(k)           => s"Missing required key: $k"
    case ParseError(k, r, h)     => s"Parse error at '$k' (value='$r'): $h"
    case ValidationError(k, m)   => s"Validation failed at '$k': $m"
    case MultiError(es)          => es.map(render).mkString("\n")
  }
}

// ---------- Result type ----------

sealed trait Validated[+A] {
  def map[B](f: A => B): Validated[B]
  def flatMap[B](f: A => Validated[B]): Validated[B]
  def orElse[B >: A](other: => Validated[B]): Validated[B]
  def getOrElse[B >: A](default: => B): B
  def errors: List[ConfigError]
  def isValid: Boolean
}

case class Valid[+A](value: A) extends Validated[A] {
  def map[B](f: A => B): Validated[B]          = Valid(f(value))
  def flatMap[B](f: A => Validated[B]): Validated[B] = f(value)
  def orElse[B >: A](other: => Validated[B]): Validated[B] = this
  def getOrElse[B >: A](default: => B): B      = value
  def errors: List[ConfigError]                = Nil
  def isValid: Boolean                         = true
}

case class Invalid(errors: List[ConfigError]) extends Validated[Nothing] {
  def map[B](f: Nothing => B): Validated[B]          = this
  def flatMap[B](f: Nothing => Validated[B]): Validated[B] = this
  def orElse[B >: Nothing](other: => Validated[B]): Validated[B] = other
  def getOrElse[B >: Nothing](default: => B): B      = default
  def isValid: Boolean                               = false
}

object Validated {
  def valid[A](a: A): Validated[A] = Valid(a)
  def invalid(errors: List[ConfigError]): Validated[Nothing] = Invalid(errors)
  def invalid(e: ConfigError): Validated[Nothing] = Invalid(List(e))

  def sequence[A](vs: List[Validated[A]]): Validated[List[A]] = {
    val (errs, vals) = vs.partitionMap {
      case Valid(a)    => Right(a)
      case Invalid(es) => Left(es)
    }
    if (errs.isEmpty) Valid(vals)
    else Invalid(errs.flatten)
  }
}

// ---------- Source abstraction ----------

trait ConfigSource {
  def getString(key: String): Option[String]
  def allKeys: Set[String]
}

class MapConfigSource(private val underlying: Map[String, String]) extends ConfigSource {
  def getString(key: String): Option[String] = underlying.get(key)
  def allKeys: Set[String] = underlying.keySet
}

object MapConfigSource {
  def apply(pairs: (String, String)*): MapConfigSource = new MapConfigSource(Map(pairs: _*))
  def fromProperties(path: String): Try[MapConfigSource] = Try {
    val props = new java.util.Properties()
    val stream = getClass.getResourceAsStream(path)
    if (stream == null) throw new IllegalArgumentException(s"Resource not found: $path")
    props.load(stream)
    val m = props.stringPropertyNames().toArray.map(_.asInstanceOf[String]).map(k => k -> props.getProperty(k)).toMap
    new MapConfigSource(m)
  }
}

// ---------- Schema / Reader ----------

trait ConfigReader[A] {
  def read(key: String, source: ConfigSource): Validated[A]
  def withDefault(default: A): ConfigReader[A] = ConfigReader.withDefault(this, default)
  def validate(pred: A => Boolean, msg: A => String): ConfigReader[A] = ConfigReader.validated(this, pred, msg)
}

object ConfigReader {
  def string: ConfigReader[String] = (key, src) =>
    src.getString(key).fold[Validated[String]](Validated.invalid(MissingKey(key)))(Validated.valid)

  def int: ConfigReader[Int] = (key, src) =>
    string.read(key, src).flatMap { raw =>
      Try(raw.trim.toInt) match {
        case Success(n) => Validated.valid(n)
        case Failure(_) => Validated.invalid(ParseError(key, raw, "expected integer"))
      }
    }

  def double: ConfigReader[Double] = (key, src) =>
    string.read(key, src).flatMap { raw =>
      Try(raw.trim.toDouble) match {
        case Success(d) => Validated.valid(d)
        case Failure(_) => Validated.invalid(ParseError(key, raw, "expected number"))
      }
    }

  def boolean: ConfigReader[Boolean] = (key, src) =>
    string.read(key, src).flatMap {
      case "true" | "1" | "yes" => Validated.valid(true)
      case "false" | "0" | "no" => Validated.valid(false)
      case raw => Validated.invalid(ParseError(key, raw, "expected boolean"))
    }

  def withDefault[A](reader: ConfigReader[A], default: A): ConfigReader[A] =
    (key, src) => reader.read(key, src) match {
      case Invalid(List(MissingKey(_))) => Validated.valid(default)
      case other => other
    }

  def validated[A](reader: ConfigReader[A], pred: A => Boolean, msg: A => String): ConfigReader[A] =
    (key, src) => reader.read(key, src).flatMap { v =>
      if (pred(v)) Validated.valid(v)
      else Validated.invalid(ValidationError(key, msg(v)))
    }

  implicit class ConfigReaderOps[A](reader: ConfigReader[A]) {
    def map[B](f: A => B): ConfigReader[B] =
      (key, src) => reader.read(key, src).map(f)
  }
}

// ---------- Config loader ----------

class ConfigLoader(source: ConfigSource) {
  def read[A](key: String)(implicit reader: ConfigReader[A]): Validated[A] =
    reader.read(key, source)

  def readAll[A](keys: List[String])(implicit reader: ConfigReader[A]): Validated[List[A]] =
    Validated.sequence(keys.map(read[A]))
}

object ConfigLoader {
  def fromMap(pairs: (String, String)*): ConfigLoader =
    new ConfigLoader(MapConfigSource(pairs: _*))
  }

  // ---------- Extension helpers ----------

  object ConfigReaderSyntax {
  implicit class SourceOps(source: ConfigSource) {
    def readString(key: String): Validated[String] = ConfigReader.string.read(key, source)
    def readInt(key: String): Validated[Int]       = ConfigReader.int.read(key, source)
    def readDouble(key: String): Validated[Double] = ConfigReader.double.read(key, source)
    def readBool(key: String): Validated[Boolean]  = ConfigReader.boolean.read(key, source)
  }
  }

  // ---------- Typed config sections ----------

  trait ConfigSection[A] {
  def prefix: String
  def load(loader: ConfigLoader): Validated[A]
  }

  case class ServerConfig(
  host: String,
  port: Int,
  maxConnections: Int,
  readTimeoutMs: Long,
  )

  case class DatabaseConfig(
  url: String,
  user: String,
  password: String,
  poolSize: Int,
  )

  case class AppConfig(
  server: ServerConfig,
  database: DatabaseConfig,
  )

  object ServerConfigSection extends ConfigSection[ServerConfig] {
  val prefix = "server"

  def load(loader: ConfigLoader): Validated[ServerConfig] = {
    val host    = ConfigReader.string.withDefault("0.0.0.0").read(s"$prefix.host", loader.source)
    val port    = ConfigReader.int.withDefault(8080).read(s"$prefix.port", loader.source)
    val maxConn = ConfigReader.int.withDefault(100).read(s"$prefix.maxConnections", loader.source)
    val timeout = ConfigReader.int.withDefault(30000).read(s"$prefix.readTimeoutMs", loader.source)
    Validated.sequence(List(host, port, maxConn, timeout)).map {
      case List(h: String, p: Int, m: Int, t: Int) => ServerConfig(h, p, m, t)
      case _ => throw new IllegalStateException("impossible")
    }
  }

  def loader: ConfigLoader = ???  // placeholder
  def source: ConfigSource = ???
  }

  object DatabaseConfigSection extends ConfigSection[DatabaseConfig] {
  val prefix = "db"

  def load(loader: ConfigLoader): Validated[DatabaseConfig] = {
    val url      = ConfigReader.string.read(s"$prefix.url", loader.source)
    val user     = ConfigReader.string.read(s"$prefix.user", loader.source)
    val password = ConfigReader.string.read(s"$prefix.password", loader.source)
    val pool     = ConfigReader.int.withDefault(10).read(s"$prefix.poolSize", loader.source)
    Validated.sequence(List(url, user, password, pool)).map {
      case List(u: String, usr: String, pw: String, p: Int) => DatabaseConfig(u, usr, pw, p)
      case _ => throw new IllegalStateException("impossible")
    }
  }

  def loader: ConfigLoader = ???  // placeholder
  def source: ConfigSource = ???
  }

  object AppConfig {
  def load(source: ConfigSource): Validated[AppConfig] = {
    val loader = new ConfigLoader(source)
    val server = ServerConfigSection.load(loader)
    val db     = DatabaseConfigSection.load(loader)
    Validated.sequence(List(server, db)).map {
      case List(s: ServerConfig, d: DatabaseConfig) => AppConfig(s, d)
      case _ => throw new IllegalStateException("impossible")
    }
  }

  def loadOrThrow(source: ConfigSource): AppConfig = load(source) match {
    case Valid(cfg)    => cfg
    case Invalid(errs) => throw new RuntimeException(ConfigError.render(MultiError(errs)))
  }
  }
