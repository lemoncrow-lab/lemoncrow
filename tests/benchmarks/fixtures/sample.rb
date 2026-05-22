# Synthetic A/B fixture — Ruby job queue with worker pool.
# NOT for production use. Generated to exercise tree-sitter outline.

require 'thread'
require 'logger'
require 'json'
require 'digest'

module JobQueue
  QUEUE_STATES = %i[pending running completed failed retrying].freeze

  class Error < StandardError; end
  class JobNotFoundError < Error; end
  class QueueShutdownError < Error; end

  class Job
    attr_reader :id, :name, :payload, :state, :attempt, :max_attempts,
                :created_at, :started_at, :finished_at, :error_message

    def initialize(name:, payload: {}, max_attempts: 3)
      @id = Digest::SHA256.hexdigest("#{name}:#{Time.now.to_f}:#{rand}")[0..15]
      @name = name
      @payload = payload
      @max_attempts = max_attempts
      @attempt = 0
      @state = :pending
      @created_at = Time.now
      @started_at = nil
      @finished_at = nil
      @error_message = nil
    end

    def start!
      @state = :running
      @started_at = Time.now
      @attempt += 1
    end

    def complete!
      @state = :completed
      @finished_at = Time.now
    end

    def fail!(message)
      @error_message = message
      @finished_at = Time.now
      if @attempt < @max_attempts
        @state = :retrying
      else
        @state = :failed
      end
    end

    def retryable?
      @state == :retrying
    end

    def duration
      return nil unless @started_at && @finished_at

      @finished_at - @started_at
    end

    def to_h
      {
        id: @id,
        name: @name,
        payload: @payload,
        state: @state,
        attempt: @attempt,
        max_attempts: @max_attempts,
        created_at: @created_at&.iso8601,
        started_at: @started_at&.iso8601,
        finished_at: @finished_at&.iso8601,
        error_message: @error_message,
        duration: duration
      }
    end

    def to_json(*)
      JSON.generate(to_h)
    end
  end

  class Registry
    def initialize
      @handlers = {}
      @mutex = Mutex.new
    end

    def register(name, handler = nil, &block)
      h = handler || block
      raise ArgumentError, 'handler must respond to :call' unless h.respond_to?(:call)

      @mutex.synchronize { @handlers[name.to_s] = h }
    end

    def lookup(name)
      @mutex.synchronize { @handlers.fetch(name.to_s) }
    rescue KeyError
      raise JobNotFoundError, "no handler registered for job '#{name}'"
    end

    def registered?(name)
      @mutex.synchronize { @handlers.key?(name.to_s) }
    end

    def names
      @mutex.synchronize { @handlers.keys.dup }
    end
  end

  class Queue
    attr_reader :name, :size

    def initialize(name: 'default', max_size: 1000)
      @name = name
      @max_size = max_size
      @queue = []
      @mutex = Mutex.new
      @cond = ConditionVariable.new
      @shutdown = false
    end

    def push(job)
      @mutex.synchronize do
        raise QueueShutdownError, 'queue is shut down' if @shutdown
        raise 'queue full' if @queue.size >= @max_size

        @queue << job
        @cond.signal
      end
    end

    def pop(timeout: nil)
      deadline = timeout ? Time.now + timeout : nil
      @mutex.synchronize do
        loop do
          return @queue.shift unless @queue.empty?
          return nil if @shutdown

          if deadline
            remaining = deadline - Time.now
            return nil if remaining <= 0

            @cond.wait(@mutex, remaining)
          else
            @cond.wait(@mutex)
          end
        end
      end
    end

    def shutdown!
      @mutex.synchronize do
        @shutdown = true
        @cond.broadcast
      end
    end

    def size
      @mutex.synchronize { @queue.size }
    end

    def empty?
      size.zero?
    end

    def drain
      @mutex.synchronize { @queue.dup }
    end
  end

  class Worker
    attr_reader :id, :stats

    def initialize(id:, queue:, registry:, logger: Logger.new($stdout))
      @id = id
      @queue = queue
      @registry = registry
      @logger = logger
      @thread = nil
      @stats = { processed: 0, failed: 0, retried: 0 }
    end

    def start
      @thread = Thread.new { run_loop }
      self
    end

    def stop
      @thread&.join
    end

    private

    def run_loop
      loop do
        job = @queue.pop(timeout: 1)
        break if job.nil? && @queue.shutdown?

        process(job) if job
      rescue QueueShutdownError
        break
      end
    end

    def process(job)
      job.start!
      handler = @registry.lookup(job.name)
      handler.call(job.payload)
      job.complete!
      @stats[:processed] += 1
      @logger.info "[Worker #{@id}] completed job #{job.id} (#{job.name})"
    rescue StandardError => e
      job.fail!(e.message)
      if job.retryable?
        @queue.push(job)
        @stats[:retried] += 1
        @logger.warn "[Worker #{@id}] retrying job #{job.id}: #{e.message}"
      else
        @stats[:failed] += 1
        @logger.error "[Worker #{@id}] job #{job.id} failed permanently: #{e.message}"
      end
    end
  end

  class Pool
    attr_reader :workers

    def initialize(size: 4, queue: nil, registry: nil, logger: Logger.new($stdout))
      @size = size
      @queue = queue || Queue.new
      @registry = registry || Registry.new
      @logger = logger
      @workers = []
    end

    def register(name, handler = nil, &block)
      @registry.register(name, handler, &block)
      self
    end

    def enqueue(name, payload = {})
      job = Job.new(name: name, payload: payload)
      @queue.push(job)
      job
    end

    def start
      @size.times do |i|
        worker = Worker.new(id: i, queue: @queue, registry: @registry, logger: @logger)
        @workers << worker.start
      end
      self
    end

    def stop
      @queue.shutdown!
      @workers.each(&:stop)
      self
    end

    def stats
      totals = { processed: 0, failed: 0, retried: 0 }
      @workers.each do |w|
        w.stats.each { |k, v| totals[k] += v }
      end
      totals.merge(queue_depth: @queue.size)
    end
  end
end
