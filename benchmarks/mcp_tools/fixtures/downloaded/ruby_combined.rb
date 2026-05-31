// --- rb_ar_base.rb ---
# frozen_string_literal: true

require "active_support/benchmarkable"
require "active_support/dependencies"
require "active_support/descendants_tracker"
require "active_support/time"
require "active_support/core_ext/class/subclasses"
require "active_record/log_subscriber"
require "active_record/explain_subscriber"
require "active_record/relation/delegation"
require "active_record/attributes"
require "active_record/type_caster"
require "active_record/database_configurations"

module ActiveRecord # :nodoc:
  # = Active Record
  #
  # Active Record objects don't specify their attributes directly, but rather infer them from
  # the table definition with which they're linked. Adding, removing, and changing attributes
  # and their type is done directly in the database. Any change is instantly reflected in the
  # Active Record objects. The mapping that binds a given Active Record class to a certain
  # database table will happen automatically in most common cases, but can be overwritten for the uncommon ones.
  #
  # See the mapping rules in table_name and the full example in link:files/activerecord/README_rdoc.html for more insight.
  #
  # == Creation
  #
  # Active Records accept constructor parameters either in a hash or as a block. The hash
  # method is especially useful when you're receiving the data from somewhere else, like an
  # HTTP request. It works like this:
  #
  #   user = User.new(name: "David", occupation: "Code Artist")
  #   user.name # => "David"
  #
  # You can also use block initialization:
  #
  #   user = User.new do |u|
  #     u.name = "David"
  #     u.occupation = "Code Artist"
  #   end
  #
  # And of course you can just create a bare object and specify the attributes after the fact:
  #
  #   user = User.new
  #   user.name = "David"
  #   user.occupation = "Code Artist"
  #
  # == Conditions
  #
  # Conditions can either be specified as a string, array, or hash representing the WHERE-part of an SQL statement.
  # The array form is to be used when the condition input is tainted and requires sanitization. The string form can
  # be used for statements that don't involve tainted data. The hash form works much like the array form, except
  # only equality and range is possible. Examples:
  #
  #   class User < ActiveRecord::Base
  #     def self.authenticate_unsafely(user_name, password)
  #       where("user_name = '#{user_name}' AND password = '#{password}'").first
  #     end
  #
  #     def self.authenticate_safely(user_name, password)
  #       where("user_name = ? AND password = ?", user_name, password).first
  #     end
  #
  #     def self.authenticate_safely_simply(user_name, password)
  #       where(user_name: user_name, password: password).first
  #     end
  #   end
  #
  # The <tt>authenticate_unsafely</tt> method inserts the parameters directly into the query
  # and is thus susceptible to SQL-injection attacks if the <tt>user_name</tt> and +password+
  # parameters come directly from an HTTP request. The <tt>authenticate_safely</tt> and
  # <tt>authenticate_safely_simply</tt> both will sanitize the <tt>user_name</tt> and +password+
  # before inserting them in the query, which will ensure that an attacker can't escape the
  # query and fake the login (or worse).
  #
  # When using multiple parameters in the conditions, it can easily become hard to read exactly
  # what the fourth or fifth question mark is supposed to represent. In those cases, you can
  # resort to named bind variables instead. That's done by replacing the question marks with
  # symbols and supplying a hash with values for the matching symbol keys:
  #
  #   Company.where(
  #     "id = :id AND name = :name AND division = :division AND created_at > :accounting_date",
  #     { id: 3, name: "37signals", division: "First", accounting_date: '2005-01-01' }
  #   ).first
  #
  # Similarly, a simple hash without a statement will generate conditions based on equality with the SQL AND
  # operator. For instance:
  #
  #   Student.where(first_name: "Harvey", status: 1)
  #   Student.where(params[:student])
  #
  # A range may be used in the hash to use the SQL BETWEEN operator:
  #
  #   Student.where(grade: 9..12)
  #
  # An array may be used in the hash to use the SQL IN operator:
  #
  #   Student.where(grade: [9,11,12])
  #
  # When joining tables, nested hashes or keys written in the form 'table_name.column_name'
  # can be used to qualify the table name of a particular condition. For instance:
  #
  #   Student.joins(:schools).where(schools: { category: 'public' })
  #   Student.joins(:schools).where('schools.category' => 'public' )
  #
  # == Overwriting default accessors
  #
  # All column values are automatically available through basic accessors on the Active Record
  # object, but sometimes you want to specialize this behavior. This can be done by overwriting
  # the default accessors (using the same name as the attribute) and calling
  # +super+ to actually change things.
  #
  #   class Song < ActiveRecord::Base
  #     # Uses an integer of seconds to hold the length of the song
  #
  #     def length=(minutes)
  #       super(minutes.to_i * 60)
  #     end
  #
  #     def length
  #       super / 60
  #     end
  #   end
  #
  # == Attribute query methods
  #
  # In addition to the basic accessors, query methods are also automatically available on the Active Record object.
  # Query methods allow you to test whether an attribute value is present.
  # Additionally, when dealing with numeric values, a query method will return false if the value is zero.
  #
  # For example, an Active Record User with the <tt>name</tt> attribute has a <tt>name?</tt> method that you can call
  # to determine whether the user has a name:
  #
  #   user = User.new(name: "David")
  #   user.name? # => true
  #
  #   anonymous = User.new(name: "")
  #   anonymous.name? # => false
  #
  # Query methods will also respect any overrides of default accessors:
  #
  #   class User
  #     # Has admin boolean column
  #     def admin
  #       false
  #     end
  #   end
  #
  #   user.update(admin: true)
  #
  #   user.read_attribute(:admin)  # => true, gets the column value
  #   user[:admin] # => true, also gets the column value
  #
  #   user.admin   # => false, due to the getter override
  #   user.admin?  # => false, due to the getter override
  #
  # == Accessing attributes before they have been typecasted
  #
  # Sometimes you want to be able to read the raw attribute data without having the column-determined
  # typecast run its course first. That can be done by using the <tt><attribute>_before_type_cast</tt>
  # accessors that all attributes have. For example, if your Account model has a <tt>balance</tt> attribute,
  # you can call <tt>account.balance_before_type_cast</tt> or <tt>account.id_before_type_cast</tt>.
  #
  # This is especially useful in validation situations where the user might supply a string for an
  # integer field and you want to display the original string back in an error message. Accessing the
  # attribute normally would typecast the string to 0, which isn't what you want.
  #
  # == Dynamic attribute-based finders
  #
  # Dynamic attribute-based finders are a mildly deprecated way of getting (and/or creating) objects
  # by simple queries without turning to SQL. They work by appending the name of an attribute
  # to <tt>find_by_</tt> like <tt>Person.find_by_user_name</tt>.
  # Instead of writing <tt>Person.find_by(user_name: user_name)</tt>, you can use
  # <tt>Person.find_by_user_name(user_name)</tt>.
  #
  # It's possible to add an exclamation point (!) on the end of the dynamic finders to get them to raise an
  # ActiveRecord::RecordNotFound error if they do not return any records,
  # like <tt>Person.find_by_last_name!</tt>.
  #
  # It's also possible to use multiple attributes in the same <tt>find_by_</tt> by separating them with
  # "_and_".
  #
  #  Person.find_by(user_name: user_name, password: password)
  #  Person.find_by_user_name_and_password(user_name, password) # with dynamic finder
  #
  # It's even possible to call these dynamic finder methods on relations and named scopes.
  #
  #   Payment.order("created_on").find_by_amount(50)
  #
  # == Saving arrays, hashes, and other non-mappable objects in text columns
  #
  # Active Record can serialize any object in text columns using YAML. To do so, you must
  # specify this with a call to the class method
  # {serialize}[rdoc-ref:AttributeMethods::Serialization::ClassMethods#serialize].
  # This makes it possible to store arrays, hashes, and other non-mappable objects without doing
  # any additional work.
  #
  #   class User < ActiveRecord::Base
  #     serialize :preferences
  #   end
  #
  #   user = User.create(preferences: { "background" => "black", "display" => large })
  #   User.find(user.id).preferences # => { "background" => "black", "display" => large }
  #
  # You can also specify a class option as the second parameter that'll raise an exception
  # if a serialized object is retrieved as a descendant of a class not in the hierarchy.
  #
  #   class User < ActiveRecord::Base
  #     serialize :preferences, Hash
  #   end
  #
  #   user = User.create(preferences: %w( one two three ))
  #   User.find(user.id).preferences    # raises SerializationTypeMismatch
  #
  # When you specify a class option, the default value for that attribute will be a new
  # instance of that class.
  #
  #   class User < ActiveRecord::Base
  #     serialize :preferences, OpenStruct
  #   end
  #
  #   user = User.new
  #   user.preferences.theme_color = "red"
  #
  #
  # == Single table inheritance
  #
  # Active Record allows inheritance by storing the name of the class in a
  # column that is named "type" by default. See ActiveRecord::Inheritance for
  # more details.
  #
  # == Connection to multiple databases in different models
  #
  # Connections are usually created through
  # {ActiveRecord::Base.establish_connection}[rdoc-ref:ConnectionHandling#establish_connection] and retrieved
  # by ActiveRecord::Base.connection. All classes inheriting from ActiveRecord::Base will use this
  # connection. But you can also set a class-specific connection. For example, if Course is an
  # ActiveRecord::Base, but resides in a different database, you can just say <tt>Course.establish_connection</tt>
  # and Course and all of its subclasses will use this connection instead.
  #
  # This feature is implemented by keeping a connection pool in ActiveRecord::Base that is
  # a hash indexed by the class. If a connection is requested, the
  # {ActiveRecord::Base.retrieve_connection}[rdoc-ref:ConnectionHandling#retrieve_connection] method
  # will go up the class-hierarchy until a connection is found in the connection pool.
  #
  # == Exceptions
  #
  # * ActiveRecordError - Generic error class and superclass of all other errors raised by Active Record.
  # * AdapterNotSpecified - The configuration hash used in
  #   {ActiveRecord::Base.establish_connection}[rdoc-ref:ConnectionHandling#establish_connection]
  #   didn't include an <tt>:adapter</tt> key.
  # * AdapterNotFound - The <tt>:adapter</tt> key used in
  #   {ActiveRecord::Base.establish_connection}[rdoc-ref:ConnectionHandling#establish_connection]
  #   specified a non-existent adapter
  #   (or a bad spelling of an existing one).
  # * AssociationTypeMismatch - The object assigned to the association wasn't of the type
  #   specified in the association definition.
  # * AttributeAssignmentError - An error occurred while doing a mass assignment through the
  #   {ActiveRecord::Base#attributes=}[rdoc-ref:AttributeAssignment#attributes=] method.
  #   You can inspect the +attribute+ property of the exception object to determine which attribute
  #   triggered the error.
  # * ConnectionNotEstablished - No connection has been established.
  #   Use {ActiveRecord::Base.establish_connection}[rdoc-ref:ConnectionHandling#establish_connection] before querying.
  # * MultiparameterAssignmentErrors - Collection of errors that occurred during a mass assignment using the
  #   {ActiveRecord::Base#attributes=}[rdoc-ref:AttributeAssignment#attributes=] method.
  #   The +errors+ property of this exception contains an array of
  #   AttributeAssignmentError
  #   objects that should be inspected to determine which attributes triggered the errors.
  # * RecordInvalid - raised by {ActiveRecord::Base#save!}[rdoc-ref:Persistence#save!] and
  #   {ActiveRecord::Base.create!}[rdoc-ref:Persistence::ClassMethods#create!]
  #   when the record is invalid.
  # * RecordNotFound - No record responded to the {ActiveRecord::Base.find}[rdoc-ref:FinderMethods#find] method.
  #   Either the row with the given ID doesn't exist or the row didn't meet the additional restrictions.
  #   Some {ActiveRecord::Base.find}[rdoc-ref:FinderMethods#find] calls do not raise this exception to signal
  #   nothing was found, please check its documentation for further details.
  # * SerializationTypeMismatch - The serialized object wasn't of the class specified as the second parameter.
  # * StatementInvalid - The database server rejected the SQL statement. The precise error is added in the message.
  #
  # *Note*: The attributes listed are class-level attributes (accessible from both the class and instance level).
  # So it's possible to assign a logger to the class through <tt>Base.logger=</tt> which will then be used by all
  # instances in the current object space.
  class Base
    extend ActiveModel::Naming

    extend ActiveSupport::Benchmarkable
    extend ActiveSupport::DescendantsTracker

    extend ConnectionHandling
    extend QueryCache::ClassMethods
    extend Querying
    extend Translation
    extend DynamicMatchers
    extend DelegatedType
    extend Explain
    extend Enum
    extend Delegation::DelegateCache
    extend Aggregations::ClassMethods

    include Core
    include Persistence
    include ReadonlyAttributes
    include ModelSchema
    include Inheritance
    include Scoping
    include Sanitization
    include AttributeAssignment
    include ActiveModel::Conversion
    include Integration
    include Validations
    include CounterCache
    include Attributes
    include Locking::Optimistic
    include Locking::Pessimistic
    include Encryption::EncryptableRecord
    include AttributeMethods
    include Callbacks
    include Timestamp
    include Associations
    include SecurePassword
    include AutosaveAssociation
    include NestedAttributes
    include Transactions
    include TouchLater
    include NoTouching
    include Reflection
    include Serialization
    include Store
    include SecureToken
    include TokenFor
    include SignedId
    include Suppressor
    include Normalization
    include Marshalling::Methods

    self.param_delimiter = "_"
  end

  ActiveSupport.run_load_hooks(:active_record, Base)
end


// --- rb_routing_mapper.rb ---
# frozen_string_literal: true

require "active_support/core_ext/hash/slice"
require "active_support/core_ext/enumerable"
require "active_support/core_ext/array/extract_options"
require "active_support/core_ext/regexp"
require "action_dispatch/routing/redirection"
require "action_dispatch/routing/endpoint"

module ActionDispatch
  module Routing
    class Mapper
      URL_OPTIONS = [:protocol, :subdomain, :domain, :host, :port]

      cattr_accessor :route_source_locations, instance_accessor: false, default: false
      cattr_accessor :backtrace_cleaner, instance_accessor: false, default: ActiveSupport::BacktraceCleaner.new

      class Constraints < Routing::Endpoint # :nodoc:
        attr_reader :app, :constraints

        SERVE = ->(app, req) { app.serve req }
        CALL  = ->(app, req) { app.call req.env }

        def initialize(app, constraints, strategy)
          # Unwrap Constraints objects. I don't actually think it's possible
          # to pass a Constraints object to this constructor, but there were
          # multiple places that kept testing children of this object. I
          # *think* they were just being defensive, but I have no idea.
          if app.is_a?(self.class)
            constraints += app.constraints
            app = app.app
          end

          @strategy = strategy

          @app, @constraints, = app, constraints
        end

        def dispatcher?; @strategy == SERVE; end

        def matches?(req)
          @constraints.all? do |constraint|
            (constraint.respond_to?(:matches?) && constraint.matches?(req)) ||
              (constraint.respond_to?(:call) && constraint.call(*constraint_args(constraint, req)))
          end
        end

        def serve(req)
          return [ 404, { Constants::X_CASCADE => "pass" }, [] ] unless matches?(req)

          @strategy.call @app, req
        end

        private
          def constraint_args(constraint, request)
            arity = if constraint.respond_to?(:arity)
              constraint.arity
            else
              constraint.method(:call).arity
            end

            if arity < 1
              []
            elsif arity == 1
              [request]
            else
              [request.path_parameters, request]
            end
          end
      end

      class Mapping # :nodoc:
        ANCHOR_CHARACTERS_REGEX = %r{\A(\\A|\^)|(\\Z|\\z|\$)\Z}
        OPTIONAL_FORMAT_REGEX = %r{(?:\(\.:format\)+|\.:format|/)\Z}

        attr_reader :path, :requirements, :defaults, :to, :default_controller,
                    :default_action, :required_defaults, :ast, :scope_options

        def self.build(scope, set, ast, controller, default_action, to, via, formatted, options_constraints, anchor, options)
          scope_params = {
            blocks: scope[:blocks] || [],
            constraints: scope[:constraints] || {},
            defaults: (scope[:defaults] || {}).dup,
            module: scope[:module],
            options: scope[:options] || {}
          }

          new set: set, ast: ast, controller: controller, default_action: default_action,
              to: to, formatted: formatted, via: via, options_constraints: options_constraints,
              anchor: anchor, scope_params: scope_params, options: scope_params[:options].merge(options)
        end

        def self.check_via(via)
          if via.empty?
            msg = "You should not use the `match` method in your router without specifying an HTTP method.\n" \
              "If you want to expose your action to both GET and POST, add `via: [:get, :post]` option.\n" \
              "If you want to expose your action to GET, use `get` in the router:\n" \
              "  Instead of: match \"controller#action\"\n" \
              "  Do: get \"controller#action\""
            raise ArgumentError, msg
          end
          via
        end

        def self.normalize_path(path, format)
          path = Mapper.normalize_path(path)

          if format == true
            "#{path}.:format"
          elsif optional_format?(path, format)
            "#{path}(.:format)"
          else
            path
          end
        end

        def self.optional_format?(path, format)
          format != false && !path.match?(OPTIONAL_FORMAT_REGEX)
        end

        def initialize(set:, ast:, controller:, default_action:, to:, formatted:, via:, options_constraints:, anchor:, scope_params:, options:)
          @defaults           = scope_params[:defaults]
          @set                = set
          @to                 = intern(to)
          @default_controller = intern(controller)
          @default_action     = intern(default_action)
          @anchor             = anchor
          @via                = via
          @internal           = options.delete(:internal)
          @scope_options      = scope_params[:options]
          ast                 = Journey::Ast.new(ast, formatted)

          options = ast.wildcard_options.merge!(options)

          options = normalize_options!(options, ast.path_params, scope_params[:module])

          split_options = constraints(options, ast.path_params)

          constraints = scope_params[:constraints].merge Hash[split_options[:constraints] || []]

          if options_constraints.is_a?(Hash)
            @defaults = Hash[options_constraints.find_all { |key, default|
              URL_OPTIONS.include?(key) && (String === default || Integer === default)
            }].merge @defaults
            @blocks = scope_params[:blocks]
            constraints.merge! options_constraints
          else
            @blocks = blocks(options_constraints)
          end

          requirements, conditions = split_constraints ast.path_params, constraints
          verify_regexp_requirements requirements, ast.wildcard_options

          formats = normalize_format(formatted)

          @requirements = formats[:requirements].merge Hash[requirements]
          @conditions = Hash[conditions]
          @defaults = formats[:defaults].merge(@defaults).merge(normalize_defaults(options))

          if ast.path_params.include?(:action) && !@requirements.key?(:action)
            @defaults[:action] ||= "index"
          end

          @required_defaults = (split_options[:required_defaults] || []).map(&:first)

          ast.requirements = @requirements
          @path = Journey::Path::Pattern.new(ast, @requirements, JOINED_SEPARATORS, @anchor)
        end

        JOINED_SEPARATORS = SEPARATORS.join # :nodoc:

        def make_route(name, precedence)
          Journey::Route.new(name: name, app: application, path: path, constraints: conditions,
                             required_defaults: required_defaults, defaults: defaults,
                             request_method_match: request_method, precedence: precedence,
                             scope_options: scope_options, internal: @internal, source_location: route_source_location)
        end

        def application
          app(@blocks)
        end

        def conditions
          build_conditions @conditions, @set.request_class
        end

        def build_conditions(current_conditions, request_class)
          conditions = current_conditions.dup

          conditions.keep_if do |k, _|
            request_class.public_method_defined?(k)
          end
        end
        private :build_conditions

        def request_method
          @via.map { |x| Journey::Route.verb_matcher(x) }
        end
        private :request_method

        private
          def intern(object)
            object.is_a?(String) ? -object : object
          end

          def normalize_options!(options, path_params, modyoule)
            if path_params.include?(:controller)
              raise ArgumentError, ":controller segment is not allowed within a namespace block" if modyoule

              # Add a default constraint for :controller path segments that matches namespaced
              # controllers with default routes like :controller/:action/:id(.:format), e.g:
              # GET /admin/products/show/1
              # => { controller: 'admin/products', action: 'show', id: '1' }
              options[:controller] ||= /.+?/
            end

            if to.respond_to?(:action) || to.respond_to?(:call)
              options
            else
              if to.nil?
                controller = default_controller
                action = default_action
              elsif to.is_a?(String) && to.include?("#")
                to_endpoint = to.split("#").map!(&:-@)
                controller  = to_endpoint[0]
                action      = to_endpoint[1]
              else
                raise ArgumentError, ":to must respond to `action` or `call`, or it must be a String that includes '#'"
              end

              controller = add_controller_module(controller, modyoule)

              options.merge! check_controller_and_action(path_params, controller, action)
            end
          end

          def split_constraints(path_params, constraints)
            constraints.partition do |key, requirement|
              path_params.include?(key) || key == :controller
            end
          end

          def normalize_format(formatted)
            case formatted
            when true
              { requirements: { format: /.+/ },
                defaults:     {} }
            when Regexp
              { requirements: { format: formatted },
                defaults:     { format: nil } }
            when String
              { requirements: { format: Regexp.compile(formatted) },
                defaults:     { format: formatted } }
            else
              { requirements: {}, defaults: {} }
            end
          end

          def verify_regexp_requirements(requirements, wildcard_options)
            requirements.each do |requirement, regex|
              next unless regex.is_a? Regexp

              if ANCHOR_CHARACTERS_REGEX.match?(regex.source)
                raise ArgumentError, "Regexp anchor characters are not allowed in routing requirements: #{requirement.inspect}"
              end

              if regex.multiline?
                next if wildcard_options.key?(requirement)

                raise ArgumentError, "Regexp multiline option is not allowed in routing requirements: #{regex.inspect}"
              end
            end
          end

          def normalize_defaults(options)
            Hash[options.reject { |_, default| Regexp === default }]
          end

          def app(blocks)
            if to.respond_to?(:action)
              Routing::RouteSet::StaticDispatcher.new to
            elsif to.respond_to?(:call)
              Constraints.new(to, blocks, Constraints::CALL)
            elsif blocks.any?
              Constraints.new(dispatcher(defaults.key?(:controller)), blocks, Constraints::SERVE)
            else
              dispatcher(defaults.key?(:controller))
            end
          end

          def check_controller_and_action(path_params, controller, action)
            hash = check_part(:controller, controller, path_params, {}) do |part|
              translate_controller(part) {
                message = +"'#{part}' is not a supported controller name. This can lead to potential routing problems."
                message << " See https://guides.rubyonrails.org/routing.html#specifying-a-controller-to-use"

                raise ArgumentError, message
              }
            end

            check_part(:action, action, path_params, hash) { |part|
              part.is_a?(Regexp) ? part : part.to_s
            }
          end

          def check_part(name, part, path_params, hash)
            if part
              hash[name] = yield(part)
            else
              unless path_params.include?(name)
                message = "Missing :#{name} key on routes definition, please check your routes."
                raise ArgumentError, message
              end
            end
            hash
          end

          def add_controller_module(controller, modyoule)
            if modyoule && !controller.is_a?(Regexp)
              if controller&.start_with?("/")
                -controller[1..-1]
              else
                -[modyoule, controller].compact.join("/")
              end
            else
              controller
            end
          end

          def translate_controller(controller)
            return controller if Regexp === controller
            return controller.to_s if /\A[a-z_0-9][a-z_0-9\/]*\z/.match?(controller)

            yield
          end

          def blocks(callable_constraint)
            unless callable_constraint.respond_to?(:call) || callable_constraint.respond_to?(:matches?)
              raise ArgumentError, "Invalid constraint: #{callable_constraint.inspect} must respond to :call or :matches?"
            end
            [callable_constraint]
          end

          def constraints(options, path_params)
            options.group_by do |key, option|
              if Regexp === option
                :constraints
              else
                if path_params.include?(key)
                  :path_params
                else
                  :required_defaults
                end
              end
            end
          end

          def dispatcher(raise_on_name_error)
            Routing::RouteSet::Dispatcher.new raise_on_name_error
          end

          def route_source_location
            if Mapper.route_source_locations
              action_dispatch_dir = File.expand_path("..", __dir__)
              caller_location = caller_locations.find { |location| !location.path.include?(action_dispatch_dir) }
              cleaned_path = Mapper.backtrace_cleaner.clean([caller_location.path]).first
              "#{cleaned_path}:#{caller_location.lineno}" if cleaned_path
            end
          end
      end

      # Invokes Journey::Router::Utils.normalize_path, then ensures that
      # /(:locale) becomes (/:locale). Except for root cases, where the
      # former is the correct one.
      def self.normalize_path(path)
        path = Journey::Router::Utils.normalize_path(path)

        # the path for a root URL at this point can be something like
        # "/(/:locale)(/:platform)/(:browser)", and we would want
        # "/(:locale)(/:platform)(/:browser)"

        # reverse "/(", "/((" etc to "(/", "((/" etc
        path.gsub!(%r{/(\(+)/?}, '\1/')
        # if a path is all optional segments, change the leading "(/" back to
        # "/(" so it evaluates to "/" when interpreted with no options.
        # Unless, however, at least one secondary segment consists of a static
        # part, ex. "(/:locale)(/pages/:page)"
        path.sub!(%r{^(\(+)/}, '/\1') if %r{^(\(+[^)]+\))(\(+/:[^)]+\))*$}.match?(path)
        path
      end

      def self.normalize_name(name)
        normalize_path(name)[1..-1].tr("/", "_")
      end

      module Base
        # Matches a URL pattern to one or more routes.
        #
        # You should not use the +match+ method in your router
        # without specifying an HTTP method.
        #
        # If you want to expose your action to both GET and POST, use:
        #
        #   # sets :controller, :action, and :id in params
        #   match ':controller/:action/:id', via: [:get, :post]
        #
        # Note that +:controller+, +:action+, and +:id+ are interpreted as URL
        # query parameters and thus available through +params+ in an action.
        #
        # If you want to expose your action to GET, use +get+ in the router:
        #
        # Instead of:
        #
        #   match ":controller/:action/:id"
        #
        # Do:
        #
        #   get ":controller/:action/:id"
        #
        # Two of these symbols are special, +:controller+ maps to the controller
        # and +:action+ to the controller's action. A pattern can also map
        # wildcard segments (globs) to params:
        #
        #   get 'songs/*category/:title', to: 'songs#show'
        #
        #   # 'songs/rock/classic/stairway-to-heaven' sets
        #   #  params[:category] = 'rock/classic'
        #   #  params[:title] = 'stairway-to-heaven'
        #
        # To match a wildcard parameter, it must have a name assigned to it.
        # Without a variable name to attach the glob parameter to, the route
        # can't be parsed.
        #
        # When a pattern points to an internal route, the route's +:action+ and
        # +:controller+ should be set in options or hash shorthand. Examples:
        #
        #   match 'photos/:id' => 'photos#show', via: :get
        #   match 'photos/:id', to: 'photos#show', via: :get
        #   match 'photos/:id', controller: 'photos', action: 'show', via: :get
        #
        # A pattern can also point to a +Rack+ endpoint i.e. anything that
        # responds to +call+:
        #
        #   match 'photos/:id', to: -> (hash) { [200, {}, ["Coming soon"]] }, via: :get
        #   match 'photos/:id', to: PhotoRackApp, via: :get
        #   # Yes, controller actions are just rack endpoints
        #   match 'photos/:id', to: PhotosController.action(:show), via: :get
        #
        # Because requesting various HTTP verbs with a single action has security
        # implications, you must either specify the actions in
        # the via options or use one of the HttpHelpers[rdoc-ref:HttpHelpers]
        # instead +match+
        #
        # === Options
        #
        # Any options not seen here are passed on as params with the URL.
        #
        # [:controller]
        #   The route's controller.
        #
        # [:action]
        #   The route's action.
        #
        # [:param]
        #   Overrides the default resource identifier +:id+ (name of the
        #   dynamic segment used to generate the routes).
        #   You can access that segment from your controller using
        #   <tt>params[<:param>]</tt>.
        #   In your router:
        #
        #      resources :users, param: :name
        #
        #   The +users+ resource here will have the following routes generated for it:
        #
        #      GET       /users(.:format)
        #      POST      /users(.:format)
        #      GET       /users/new(.:format)
        #      GET       /users/:name/edit(.:format)
        #      GET       /users/:name(.:format)
        #      PATCH/PUT /users/:name(.:format)
        #      DELETE    /users/:name(.:format)
        #
        #   You can override <tt>ActiveRecord::Base#to_param</tt> of a related
        #   model to construct a URL:
        #
        #      class User < ActiveRecord::Base
        #        def to_param
        #          name
        #        end
        #      end
        #
        #      user = User.find_by(name: 'Phusion')
        #      user_path(user)  # => "/users/Phusion"
        #
        # [:path]
        #   The path prefix for the routes.
        #
        # [:module]
        #   The namespace for :controller.
        #
        #     match 'path', to: 'c#a', module: 'sekret', controller: 'posts', via: :get
        #     # => Sekret::PostsController
        #
        #   See <tt>Scoping#namespace</tt> for its scope equivalent.
        #
        # [:as]
        #   The name used to generate routing helpers.
        #
        # [:via]
        #   Allowed HTTP verb(s) for route.
        #
        #      match 'path', to: 'c#a', via: :get
        #      match 'path', to: 'c#a', via: [:get, :post]
        #      match 'path', to: 'c#a', via: :all
        #
        # [:to]
        #   Points to a +Rack+ endpoint. Can be an object that responds to
        #   +call+ or a string representing a controller's action.
        #
        #      match 'path', to: 'controller#action', via: :get
        #      match 'path', to: -> (env) { [200, {}, ["Success!"]] }, via: :get
        #      match 'path', to: RackApp, via: :get
        #
        # [:on]
        #   Shorthand for wrapping routes in a specific RESTful context. Valid
        #   values are +:member+, +:collection+, and +:new+. Only use within
        #   <tt>resource(s)</tt> block. For example:
        #
        #      resource :bar do
        #        match 'foo', to: 'c#a', on: :member, via: [:get, :post]
        #      end
        #
        #   Is equivalent to:
        #
        #      resource :bar do
        #        member do
        #          match 'foo', to: 'c#a', via: [:get, :post]
        #        end
        #      end
        #
        # [:constraints]
        #   Constrains parameters with a hash of regular expressions
        #   or an object that responds to <tt>matches?</tt>. In addition, constraints
        #   other than path can also be specified with any object
        #   that responds to <tt>===</tt> (e.g. String, Array, Range, etc.).
        #
        #     match 'path/:id', constraints: { id: /[A-Z]\d{5}/ }, via: :get
        #
        #     match 'json_only', constraints: { format: 'json' }, via: :get
        #
        #     class PermitList
        #       def matches?(request) request.remote_ip == '1.2.3.4' end
        #     end
        #     match 'path', to: 'c#a', constraints: PermitList.new, via: :get
        #
        #   See <tt>Scoping#constraints</tt> for more examples with its scope
        #   equivalent.
        #
        # [:defaults]
        #   Sets defaults for parameters
        #
        #     # Sets params[:format] to 'jpg' by default
        #     match 'path', to: 'c#a', defaults: { format: 'jpg' }, via: :get
        #
        #   See <tt>Scoping#defaults</tt> for its scope equivalent.
        #
        # [:anchor]
        #   Boolean to anchor a <tt>match</tt> pattern. Default is true. When set to
        #   false, the pattern matches any request prefixed with the given path.
        #
        #     # Matches any request starting with 'path'
        #     match 'path', to: 'c#a', anchor: false, via: :get
        #
        # [:format]
        #   Allows you to specify the default value for optional +format+
        #   segment or disable it by supplying +false+.
        def match(path, options = nil)
        end

        # Mount a Rack-based application to be used within the application.
        #
        #   mount SomeRackApp, at: "some_route"
        #
        # Alternatively:
        #
        #   mount(SomeRackApp => "some_route")
        #
        # For options, see +match+, as +mount+ uses it internally.
        #
        # All mounted applications come with routing helpers to access them.
        # These are named after the class specified, so for the above example
        # the helper is either +some_rack_app_path+ or +some_rack_app_url+.
        # To customize this helper's name, use the +:as+ option:
        #
        #   mount(SomeRackApp => "some_route", as: "exciting")
        #
        # This will generate the +exciting_path+ and +exciting_url+ helpers
        # which can be used to navigate to this mounted app.
        def mount(app, options = nil)
          if options
            path = options.delete(:at)
          elsif Hash === app
            options = app
            app, path = options.find { |k, _| k.respond_to?(:call) }
            options.delete(app) if app
          end

          raise ArgumentError, "A rack application must be specified" unless app.respond_to?(:call)
          raise ArgumentError, <<~MSG unless path
            Must be called with mount point

              mount SomeRackApp, at: "some_route"
              or
              mount(SomeRackApp => "some_route")
          MSG

          rails_app = rails_app? app
          options[:as] ||= app_name(app, rails_app)

          target_as       = name_for_action(options[:as], path)
          options[:via] ||= :all

          match(path, { to: app, anchor: false, format: false }.merge(options))

          define_generate_prefix(app, target_as) if rails_app
          self
        end

        def default_url_options=(options)
          @set.default_url_options = options
        end
        alias_method :default_url_options, :default_url_options=

        def with_default_scope(scope, &block)
          scope(scope) do
            instance_exec(&block)
          end
        end

        # Query if the following named route was already defined.
        def has_named_route?(name)
          @set.named_routes.key?(name)
        end

        private
          def rails_app?(app)
            app.is_a?(Class) && app < Rails::Railtie
          end

          def app_name(app, rails_app)
            if rails_app
              app.railtie_name
            elsif app.is_a?(Class)
              class_name = app.name
              ActiveSupport::Inflector.underscore(class_name).tr("/", "_")
            end
          end

          def define_generate_prefix(app, name)
            _route = @set.named_routes.get name
            _routes = @set
            _url_helpers = @set.url_helpers

            script_namer = ->(options) do
              prefix_options = options.slice(*_route.segment_keys)
              prefix_options[:script_name] = "" if options[:original_script_name]

              if options[:_recall]
                prefix_options.reverse_merge!(options[:_recall].slice(*_route.segment_keys))
              end

              # We must actually delete prefix segment keys to avoid passing them to next url_for.
              _route.segment_keys.each { |k| options.delete(k) }
              _url_helpers.public_send("#{name}_path", prefix_options)
            end

            app.routes.define_mounted_helper(name, script_namer)

            app.routes.extend Module.new {
              def optimize_routes_generation?; false; end

              define_method :find_script_name do |options|
                if options.key? :script_name
                  super(options)
                else
                  script_namer.call(options)
                end
              end
            }
          end
      end

      module HttpHelpers
        # Define a route that only recognizes HTTP GET.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   get 'bacon', to: 'food#bacon'
        def get(*args, &block)
          map_method(:get, args, &block)
        end

        # Define a route that only recognizes HTTP POST.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   post 'bacon', to: 'food#bacon'
        def post(*args, &block)
          map_method(:post, args, &block)
        end

        # Define a route that only recognizes HTTP PATCH.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   patch 'bacon', to: 'food#bacon'
        def patch(*args, &block)
          map_method(:patch, args, &block)
        end

        # Define a route that only recognizes HTTP PUT.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   put 'bacon', to: 'food#bacon'
        def put(*args, &block)
          map_method(:put, args, &block)
        end

        # Define a route that only recognizes HTTP DELETE.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   delete 'broccoli', to: 'food#broccoli'
        def delete(*args, &block)
          map_method(:delete, args, &block)
        end

        # Define a route that only recognizes HTTP OPTIONS.
        # For supported arguments, see match[rdoc-ref:Base#match]
        #
        #   options 'carrots', to: 'food#carrots'
        def options(*args, &block)
          map_method(:options, args, &block)
        end

        private
          def map_method(method, args, &block)
            options = args.extract_options!
            options[:via] = method
            match(*args, options, &block)
            self
          end
      end

      # You may wish to organize groups of controllers under a namespace.
      # Most commonly, you might group a number of administrative controllers
      # under an +admin+ namespace. You would place these controllers under
      # the <tt>app/controllers/admin</tt> directory, and you can group them
      # together in your router:
      #
      #   namespace "admin" do
      #     resources :posts, :comments
      #   end
      #
      # This will create a number of routes for each of the posts and comments
      # controller. For +Admin::PostsController+, \Rails will create:
      #
      #   GET       /admin/posts
      #   GET       /admin/posts/new
      #   POST      /admin/posts
      #   GET       /admin/posts/1
      #   GET       /admin/posts/1/edit
      #   PATCH/PUT /admin/posts/1
      #   DELETE    /admin/posts/1
      #
      # If you want to route /posts (without the prefix /admin) to
      # +Admin::PostsController+, you could use
      #
      #   scope module: "admin" do
      #     resources :posts
      #   end
      #
      # or, for a single case
      #
      #   resources :posts, module: "admin"
      #
      # If you want to route /admin/posts to +PostsController+
      # (without the <tt>Admin::</tt> module prefix), you could use
      #
      #   scope "/admin" do
      #     resources :posts
      #   end
      #
      # or, for a single case
      #
      #   resources :posts, path: "/admin/posts"
      #
      # In each of these cases, the named routes remain the same as if you did
      # not use scope. In the last case, the following paths map to
      # +PostsController+:
      #
      #   GET       /admin/posts
      #   GET       /admin/posts/new
      #   POST      /admin/posts
      #   GET       /admin/posts/1
      #   GET       /admin/posts/1/edit
      #   PATCH/PUT /admin/posts/1
      #   DELETE    /admin/posts/1
      module Scoping
        # Scopes a set of routes to the given default options.
        #
        # Take the following route definition as an example:
        #
        #   scope path: ":account_id", as: "account" do
        #     resources :projects
        #   end
        #
        # This generates helpers such as +account_projects_path+, just like +resources+ does.
        # The difference here being that the routes generated are like /:account_id/projects,
        # rather than /accounts/:account_id/projects.
        #
        # === Options
        #
        # Takes same options as <tt>Base#match</tt> and <tt>Resources#resources</tt>.
        #
        #   # route /posts (without the prefix /admin) to +Admin::PostsController+
        #   scope module: "admin" do
        #     resources :posts
        #   end
        #
        #   # prefix the posts resource's requests with '/admin'
        #   scope path: "/admin" do
        #     resources :posts
        #   end
        #
        #   # prefix the routing helper name: +sekret_posts_path+ instead of +posts_path+
        #   scope as: "sekret" do
        #     resources :posts
        #   end
        def scope(*args)
          options = args.extract_options!.dup
          scope = {}

          options[:path] = args.flatten.join("/") if args.any?
          options[:constraints] ||= {}

          unless nested_scope?
            options[:shallow_path] ||= options[:path] if options.key?(:path)
            options[:shallow_prefix] ||= options[:as] if options.key?(:as)
          end

          if options[:constraints].is_a?(Hash)
            defaults = options[:constraints].select do |k, v|
              URL_OPTIONS.include?(k) && (v.is_a?(String) || v.is_a?(Integer))
            end

            options[:defaults] = defaults.merge(options[:defaults] || {})
          else
            block, options[:constraints] = options[:constraints], {}
          end

          if options.key?(:only) || options.key?(:except)
            scope[:action_options] = { only: options.delete(:only),
                                       except: options.delete(:except) }
          end

          if options.key? :anchor
            raise ArgumentError, "anchor is ignored unless passed to `match`"
          end

          @scope.options.each do |option|
            if option == :blocks
              value = block
            elsif option == :options
              value = options
            else
              value = options.delete(option) { POISON }
            end

            unless POISON == value
              scope[option] = send("merge_#{option}_scope", @scope[option], value)
            end
          end

          @scope = @scope.new scope
          yield
          self
        ensure
          @scope = @scope.parent
        end

        POISON = Object.new # :nodoc:

        # Scopes routes to a specific controller
        #
        #   controller "food" do
        #     match "bacon", action: :bacon, via: :get
        #   end
        def controller(controller)
          @scope = @scope.new(controller: controller)
          yield
        ensure
          @scope = @scope.parent
        end

        # Scopes routes to a specific namespace. For example:
        #
        #   namespace :admin do
        #     resources :posts
        #   end
        #
        # This generates the following routes:
        #
        #       admin_posts GET       /admin/posts(.:format)          admin/posts#index
        #       admin_posts POST      /admin/posts(.:format)          admin/posts#create
        #    new_admin_post GET       /admin/posts/new(.:format)      admin/posts#new
        #   edit_admin_post GET       /admin/posts/:id/edit(.:format) admin/posts#edit
        #        admin_post GET       /admin/posts/:id(.:format)      admin/posts#show
        #        admin_post PATCH/PUT /admin/posts/:id(.:format)      admin/posts#update
        #        admin_post DELETE    /admin/posts/:id(.:format)      admin/posts#destroy
        #
        # === Options
        #
        # The +:path+, +:as+, +:module+, +:shallow_path+, and +:shallow_prefix+
        # options all default to the name of the namespace.
        #
        # For options, see <tt>Base#match</tt>. For +:shallow_path+ option, see
        # <tt>Resources#resources</tt>.
        #
        #   # accessible through /sekret/posts rather than /admin/posts
        #   namespace :admin, path: "sekret" do
        #     resources :posts
        #   end
        #
        #   # maps to +Sekret::PostsController+ rather than +Admin::PostsController+
        #   namespace :admin, module: "sekret" do
        #     resources :posts
        #   end
        #
        #   # generates +sekret_posts_path+ rather than +admin_posts_path+
        #   namespace :admin, as: "sekret" do
        #     resources :posts
        #   end
        def namespace(path, options = {}, &block)
          path = path.to_s

          defaults = {
            module:         path,
            as:             options.fetch(:as, path),
            shallow_path:   options.fetch(:path, path),
            shallow_prefix: options.fetch(:as, path)
          }

          path_scope(options.delete(:path) { path }) do
            scope(defaults.merge!(options), &block)
          end
        end

        # === Parameter Restriction
        # Allows you to constrain the nested routes based on a set of rules.
        # For instance, in order to change the routes to allow for a dot character in the +id+ parameter:
        #
        #   constraints(id: /\d+\.\d+/) do
        #     resources :posts
        #   end
        #
        # Now routes such as +/posts/1+ will no longer be valid, but +/posts/1.1+ will be.
        # The +id+ parameter must match the constraint passed in for this example.
        #
        # You may use this to also restrict other parameters:
        #
        #   resources :posts do
        #     constraints(post_id: /\d+\.\d+/) do
        #       resources :comments
        #     end
        #   end
        #
        # === Restricting based on IP
        #
        # Routes can also be constrained to an IP or a certain range of IP addresses:
        #
        #   constraints(ip: /192\.168\.\d+\.\d+/) do
        #     resources :posts
        #   end
        #
        # Any user connecting from the 192.168.* range will be able to see this resource,
        # where as any user connecting outside of this range will be told there is no such route.
        #
        # === Dynamic request matching
        #
        # Requests to routes can be constrained based on specific criteria:
        #
        #    constraints(-> (req) { /iPhone/.match?(req.env["HTTP_USER_AGENT"]) }) do
        #      resources :iphones
        #    end
        #
        # You are able to move this logic out into a class if it is too complex for routes.
        # This class must have a +matches?+ method defined on it which either returns +true+
        # if the user should be given access to that route, or +false+ if the user should not.
        #
        #    class Iphone
        #      def self.matches?(request)
        #        /iPhone/.match?(request.env["HTTP_USER_AGENT"])
        #      end
        #    end
        #
        # An expected place for this code would be +lib/constraints+.
        #
        # This class is then used like this:
        #
        #    constraints(Iphone) do
        #      resources :iphones
        #    end
        def constraints(constraints = {}, &block)
          scope(constraints: constraints, &block)
        end

        # Allows you to set default parameters for a route, such as this:
        #   defaults id: 'home' do
        #     match 'scoped_pages/(:id)', to: 'pages#show'
        #   end
        # Using this, the +:id+ parameter here will default to 'home'.
        def defaults(defaults = {})
          @scope = @scope.new(defaults: merge_defaults_scope(@scope[:defaults], defaults))
          yield
        ensure
          @scope = @scope.parent
        end

        private
          def merge_path_scope(parent, child)
            Mapper.normalize_path("#{parent}/#{child}")
          end

          def merge_shallow_path_scope(parent, child)
            Mapper.normalize_path("#{parent}/#{child}")
          end

          def merge_as_scope(parent, child)
            parent ? "#{parent}_#{child}" : child
          end

          def merge_shallow_prefix_scope(parent, child)
            parent ? "#{parent}_#{child}" : child
          end

          def merge_module_scope(parent, child)
            parent ? "#{parent}/#{child}" : child
          end

          def merge_controller_scope(parent, child)
            child
          end

          def merge_action_scope(parent, child)
            child
          end

          def merge_via_scope(parent, child)
            child
          end

          def merge_format_scope(parent, child)
            child
          end

          def merge_path_names_scope(parent, child)
            merge_options_scope(parent, child)
          end

          def merge_constraints_scope(parent, child)
            merge_options_scope(parent, child)
          end

          def merge_defaults_scope(parent, child)
            merge_options_scope(parent, child)
          end

          def merge_blocks_scope(parent, child)
            merged = parent ? parent.dup : []
            merged << child if child
            merged
          end

          def merge_options_scope(parent, child)
            (parent || {}).merge(child)
          end

          def merge_shallow_scope(parent, child)
            child ? true : false
          end

          def merge_to_scope(parent, child)
            child
          end
      end

      # Resource routing allows you to quickly declare all of the common routes
      # for a given resourceful controller. Instead of declaring separate routes
      # for your +index+, +show+, +new+, +edit+, +create+, +update+, and +destroy+
      # actions, a resourceful route declares them in a single line of code:
      #
      #  resources :photos
      #
      # Sometimes, you have a resource that clients always look up without
      # referencing an ID. A common example, /profile always shows the profile of
      # the currently logged in user. In this case, you can use a singular resource
      # to map /profile (rather than /profile/:id) to the show action.
      #
      #  resource :profile
      #
      # It's common to have resources that are logically children of other
      # resources:
      #
      #   resources :magazines do
      #     resources :ads
      #   end
      #
      # You may wish to organize groups of controllers under a namespace. Most
      # commonly, you might group a number of administrative controllers under
      # an +admin+ namespace. You would place these controllers under the
      # <tt>app/controllers/admin</tt> directory, and you can group them together
      # in your router:
      #
      #   namespace "admin" do
      #     resources :posts, :comments
      #   end
      #
      # By default the +:id+ parameter doesn't accept dots. If you need to
      # use dots as part of the +:id+ parameter add a constraint which
      # overrides this restriction, e.g:
      #
      #   resources :articles, id: /[^\/]+/
      #
      # This allows any character other than a slash as part of your +:id+.
      #
      module Resources
        # CANONICAL_ACTIONS holds all actions that does not need a prefix or
        # a path appended since they fit properly in their scope level.
        VALID_ON_OPTIONS  = [:new, :collection, :member]
        RESOURCE_OPTIONS  = [:as, :controller, :path, :only, :except, :param, :concerns]
        CANONICAL_ACTIONS = %w(index create new show update destroy)

        class Resource # :nodoc:
          attr_reader :controller, :path, :param

          def initialize(entities, api_only, shallow, options = {})
            if options[:param].to_s.include?(":")
              raise ArgumentError, ":param option can't contain colons"
            end

            @name       = entities.to_s
            @path       = (options[:path] || @name).to_s
            @controller = (options[:controller] || @name).to_s
            @as         = options[:as]
            @param      = (options[:param] || :id).to_sym
            @options    = options
            @shallow    = shallow
            @api_only   = api_only
            @only       = options.delete :only
            @except     = options.delete :except
          end

          def default_actions
            if @api_only
              [:index, :create, :show, :update, :destroy]
            else
              [:index, :create, :new, :show, :update, :destroy, :edit]
            end
          end

          def actions
            if @except
              available_actions - Array(@except).map(&:to_sym)
            else
              available_actions
            end
          end

          def available_actions
            if @only
              Array(@only).map(&:to_sym)
            else
              default_actions
            end
          end

          def name
            @as || @name
          end

          def plural
            @plural ||= name.to_s
          end

          def singular
            @singular ||= name.to_s.singularize
          end

          alias :member_name :singular

          # Checks for uncountable plurals, and appends "_index" if the plural
          # and singular form are the same.
          def collection_name
            singular == plural ? "#{plural}_index" : plural
          end

          def resource_scope
            controller
          end

          alias :collection_scope :path

          def member_scope
            "#{path}/:#{param}"
          end

          alias :shallow_scope :member_scope

          def new_scope(new_path)
            "#{path}/#{new_path}"
          end

          def nested_param
            :"#{singular}_#{param}"
          end

          def nested_scope
            "#{path}/:#{nested_param}"
          end

          def shallow?
            @shallow
          end

          def singleton?; false; end
        end

        class SingletonResource < Resource # :nodoc:
          def initialize(entities, api_only, shallow, options)
            super
            @as         = nil
            @controller = (options[:controller] || plural).to_s
            @as         = options[:as]
          end

          def default_actions
            if @api_only
              [:show, :create, :update, :destroy]
            else
              [:show, :create, :update, :destroy, :new, :edit]
            end
          end

          def plural
            @plural ||= name.to_s.pluralize
          end

          def singular
            @singular ||= name.to_s
          end

          alias :member_name :singular
          alias :collection_name :singular

          alias :member_scope :path
          alias :nested_scope :path

          def singleton?; true; end
        end

        def resources_path_names(options)
          @scope[:path_names].merge!(options)
        end

        # Sometimes, you have a resource that clients always look up without
        # referencing an ID. A common example, /profile always shows the
        # profile of the currently logged in user. In this case, you can use
        # a singular resource to map /profile (rather than /profile/:id) to
        # the show action:
        #
        #   resource :profile
        #
        # This creates six different routes in your application, all mapping to
        # the +Profiles+ controller (note that the controller is named after
        # the plural):
        #
        #   GET       /profile/new
        #   GET       /profile
        #   GET       /profile/edit
        #   PATCH/PUT /profile
        #   DELETE    /profile
        #   POST      /profile
        #
        # If you want instances of a model to work with this resource via
        # record identification (e.g. in +form_with+ or +redirect_to+), you
        # will need to call resolve[rdoc-ref:CustomUrls#resolve]:
        #
        #   resource :profile
        #   resolve('Profile') { [:profile] }
        #
        #   # Enables this to work with singular routes:
        #   form_with(model: @profile) {}
        #
        # === Options
        # Takes same options as resources[rdoc-ref:#resources]
        def resource(*resources, &block)
          options = resources.extract_options!.dup

          if apply_common_behavior_for(:resource, resources, options, &block)
            return self
          end

          with_scope_level(:resource) do
            options = apply_action_options options
            resource_scope(SingletonResource.new(resources.pop, api_only?, @scope[:shallow], options)) do
              yield if block_given?

              concerns(options[:concerns]) if options[:concerns]

              new do
                get :new
              end if parent_resource.actions.include?(:new)

              set_member_mappings_for_resource

              collection do
                post :create
              end if parent_resource.actions.include?(:create)
            end
          end

          self
        end

        # In \Rails, a resourceful route provides a mapping between HTTP verbs
        # and URLs and controller actions. By convention, each action also maps
        # to particular CRUD operations in a database. A single entry in the
        # routing file, such as
        #
        #   resources :photos
        #
        # creates seven different routes in your application, all mapping to
        # the +Photos+ controller:
        #
        #   GET       /photos
        #   GET       /photos/new
        #   POST      /photos
        #   GET       /photos/:id
        #   GET       /photos/:id/edit
        #   PATCH/PUT /photos/:id
        #   DELETE    /photos/:id
        #
        # Resources can also be nested infinitely by using this block syntax:
        #
        #   resources :photos do
        #     resources :comments
        #   end
        #
        # This generates the following comments routes:
        #
        #   GET       /photos/:photo_id/comments
        #   GET       /photos/:photo_id/comments/new
        #   POST      /photos/:photo_id/comments
        #   GET       /photos/:photo_id/comments/:id
        #   GET       /photos/:photo_id/comments/:id/edit
        #   PATCH/PUT /photos/:photo_id/comments/:id
        #   DELETE    /photos/:photo_id/comments/:id
        #
        # === Options
        # Takes same options as match[rdoc-ref:Base#match] as well as:
        #
        # [:path_names]
        #   Allows you to change the segment component of the +edit+ and +new+ actions.
        #   Actions not specified are not changed.
        #
        #     resources :posts, path_names: { new: "brand_new" }
        #
        #   The above example will now change /posts/new to /posts/brand_new.
        #
        # [:path]
        #   Allows you to change the path prefix for the resource.
        #
        #     resources :posts, path: 'postings'
        #
        #   The resource and all segments will now route to /postings instead of /posts.
        #
        # [:only]
        #   Only generate routes for the given actions.
        #
        #     resources :cows, only: :show
        #     resources :cows, only: [:show, :index]
        #
        # [:except]
        #   Generate all routes except for the given actions.
        #
        #     resources :cows, except: :show
        #     resources :cows, except: [:show, :index]
        #
        # [:shallow]
        #   Generates shallow routes for nested resource(s). When placed on a parent resource,
        #   generates shallow routes for all nested resources.
        #
        #     resources :posts, shallow: true do
        #       resources :comments
        #     end
        #
        #   Is the same as:
        #
        #     resources :posts do
        #       resources :comments, except: [:show, :edit, :update, :destroy]
        #     end
        #     resources :comments, only: [:show, :edit, :update, :destroy]
        #
        #   This allows URLs for resources that otherwise would be deeply nested such
        #   as a comment on a blog post like <tt>/posts/a-long-permalink/comments/1234</tt>
        #   to be shortened to just <tt>/comments/1234</tt>.
        #
        #   Set <tt>shallow: false</tt> on a child resource to ignore a parent's shallow parameter.
        #
        # [:shallow_path]
        #   Prefixes nested shallow routes with the specified path.
        #
        #     scope shallow_path: "sekret" do
        #       resources :posts do
        #         resources :comments, shallow: true
        #       end
        #     end
        #
        #   The +comments+ resource here will have the following routes generated for it:
        #
        #     post_comments    GET       /posts/:post_id/comments(.:format)
        #     post_comments    POST      /posts/:post_id/comments(.:format)
        #     new_post_comment GET       /posts/:post_id/comments/new(.:format)
        #     edit_comment     GET       /sekret/comments/:id/edit(.:format)
        #     comment          GET       /sekret/comments/:id(.:format)
        #     comment          PATCH/PUT /sekret/comments/:id(.:format)
        #     comment          DELETE    /sekret/comments/:id(.:format)
        #
        # [:shallow_prefix]
        #   Prefixes nested shallow route names with specified prefix.
        #
        #     scope shallow_prefix: "sekret" do
        #       resources :posts do
        #         resources :comments, shallow: true
        #       end
        #     end
        #
        #   The +comments+ resource here will have the following routes generated for it:
        #
        #     post_comments           GET       /posts/:post_id/comments(.:format)
        #     post_comments           POST      /posts/:post_id/comments(.:format)
        #     new_post_comment        GET       /posts/:post_id/comments/new(.:format)
        #     edit_sekret_comment     GET       /comments/:id/edit(.:format)
        #     sekret_comment          GET       /comments/:id(.:format)
        #     sekret_comment          PATCH/PUT /comments/:id(.:format)
        #     sekret_comment          DELETE    /comments/:id(.:format)
        #
        # [:format]
        #   Allows you to specify the default value for optional +format+
        #   segment or disable it by supplying +false+.
        #
        # [:param]
        #   Allows you to override the default param name of +:id+ in the URL.
        #
        # === Examples
        #
        #   # routes call +Admin::PostsController+
        #   resources :posts, module: "admin"
        #
        #   # resource actions are at /admin/posts.
        #   resources :posts, path: "admin/posts"
        def resources(*resources, &block)
          options = resources.extract_options!.dup

          if apply_common_behavior_for(:resources, resources, options, &block)
            return self
          end

          with_scope_level(:resources) do
            options = apply_action_options options
            resource_scope(Resource.new(resources.pop, api_only?, @scope[:shallow], options)) do
              yield if block_given?

              concerns(options[:concerns]) if options[:concerns]

              collection do
                get  :index if parent_resource.actions.include?(:index)
                post :create if parent_resource.actions.include?(:create)
              end

              new do
                get :new
              end if parent_resource.actions.include?(:new)

              set_member_mappings_for_resource
            end
          end

          self
        end

        # To add a route to the collection:
        #
        #   resources :photos do
        #     collection do
        #       get 'search'
        #     end
        #   end
        #
        # This will enable \Rails to recognize paths such as <tt>/photos/search</tt>
        # with GET, and route to the search action of +PhotosController+. It will also
        # create the <tt>search_photos_url</tt> and <tt>search_photos_path</tt>
        # route helpers.
        def collection(&block)
          unless resource_scope?
            raise ArgumentError, "can't use collection outside resource(s) scope"
          end

          with_scope_level(:collection) do
            path_scope(parent_resource.collection_scope, &block)
          end
        end

        # To add a member route, add a member block into the resource block:
        #
        #   resources :photos do
        #     member do
        #       get 'preview'
        #     end
        #   end
        #
        # This will recognize <tt>/photos/1/preview</tt> with GET, and route to the
        # preview action of +PhotosController+. It will also create the
        # <tt>preview_photo_url</tt> and <tt>preview_photo_path</tt> helpers.
        def member(&block)
          unless resource_scope?
            raise ArgumentError, "can't use member outside resource(s) scope"
          end

          with_scope_level(:member) do
            if shallow?
              shallow_scope {
                path_scope(parent_resource.member_scope, &block)
              }
            else
              path_scope(parent_resource.member_scope, &block)
            end
          end
        end

        def new(&block)
          unless resource_scope?
            raise ArgumentError, "can't use new outside resource(s) scope"
          end

          with_scope_level(:new) do
            path_scope(parent_resource.new_scope(action_path(:new)), &block)
          end
        end

        def nested(&block)
          unless resource_scope?
            raise ArgumentError, "can't use nested outside resource(s) scope"
          end

          with_scope_level(:nested) do
            if shallow? && shallow_nesting_depth >= 1
              shallow_scope do
                path_scope(parent_resource.nested_scope) do
                  scope(nested_options, &block)
                end
              end
            else
              path_scope(parent_resource.nested_scope) do
                scope(nested_options, &block)
              end
            end
          end
        end

        # See ActionDispatch::Routing::Mapper::Scoping#namespace.
        def namespace(path, options = {})
          if resource_scope?
            nested { super }
          else
            super
          end
        end

        def shallow
          @scope = @scope.new(shallow: true)
          yield
        ensure
          @scope = @scope.parent
        end

        def shallow?
          !parent_resource.singleton? && @scope[:shallow]
        end

        # Loads another routes file with the given +name+ located inside the
        # +config/routes+ directory. In that file, you can use the normal
        # routing DSL, but <i>do not</i> surround it with a
        # +Rails.application.routes.draw+ block.
        #
        #   # config/routes.rb
        #   Rails.application.routes.draw do
        #     draw :admin                 # Loads `config/routes/admin.rb`
        #     draw "third_party/some_gem" # Loads `config/routes/third_party/some_gem.rb`
        #   end
        #
        #   # config/routes/admin.rb
        #   namespace :admin do
        #     resources :accounts
        #   end
        #
        #   # config/routes/third_party/some_gem.rb
        #   mount SomeGem::Engine, at: "/some_gem"
        #
        # <b>CAUTION:</b> Use this feature with care. Having multiple routes
        # files can negatively impact discoverability and readability. For most
        # applications — even those with a few hundred routes — it's easier for
        # developers to have a single routes file.
        def draw(name)
          path = @draw_paths.find do |_path|
            File.exist? "#{_path}/#{name}.rb"
          end

          unless path
            msg  = "Your router tried to #draw the external file #{name}.rb,\n" \
                   "but the file was not found in:\n\n"
            msg += @draw_paths.map { |_path| " * #{_path}" }.join("\n")
            raise ArgumentError, msg
          end

          route_path = "#{path}/#{name}.rb"
          instance_eval(File.read(route_path), route_path.to_s)
        end

        # Matches a URL pattern to one or more routes.
        # For more information, see match[rdoc-ref:Base#match].
        #
        #   match 'path' => 'controller#action', via: :patch
        #   match 'path', to: 'controller#action', via: :post
        #   match 'path', 'otherpath', on: :member, via: :get
        def match(path, *rest, &block)
          if rest.empty? && Hash === path
            options  = path
            path, to = options.find { |name, _value| name.is_a?(String) }

            raise ArgumentError, "Route path not specified" if path.nil?

            case to
            when Symbol
              options[:action] = to
            when String
              if to.include?("#")
                options[:to] = to
              else
                options[:controller] = to
              end
            else
              options[:to] = to
            end

            options.delete(path)
            paths = [path]
          else
            options = rest.pop || {}
            paths = [path] + rest
          end

          if options.key?(:defaults)
            defaults(options.delete(:defaults)) { map_match(paths, options, &block) }
          else
            map_match(paths, options, &block)
          end
        end

        # You can specify what \Rails should route "/" to with the root method:
        #
        #   root to: 'pages#main'
        #
        # For options, see +match+, as +root+ uses it internally.
        #
        # You can also pass a string which will expand
        #
        #   root 'pages#main'
        #
        # You should put the root route at the top of <tt>config/routes.rb</tt>,
        # because this means it will be matched first. As this is the most popular route
        # of most \Rails applications, this is beneficial.
        def root(path, options = {})
          if path.is_a?(String)
            options[:to] = path
          elsif path.is_a?(Hash) && options.empty?
            options = path
          else
            raise ArgumentError, "must be called with a path and/or options"
          end

          if @scope.resources?
            with_scope_level(:root) do
              path_scope(parent_resource.path) do
                match_root_route(options)
              end
            end
          else
            match_root_route(options)
          end
        end

        private
          def parent_resource
            @scope[:scope_level_resource]
          end

          def apply_common_behavior_for(method, resources, options, &block)
            if resources.length > 1
              resources.each { |r| public_send(method, r, options, &block) }
              return true
            end

            if options[:shallow]
              options.delete(:shallow)
              shallow do
                public_send(method, resources.pop, options, &block)
              end
              return true
            end

            if resource_scope?
              nested { public_send(method, resources.pop, options, &block) }
              return true
            end

            options.keys.each do |k|
              (options[:constraints] ||= {})[k] = options.delete(k) if options[k].is_a?(Regexp)
            end

            scope_options = options.slice!(*RESOURCE_OPTIONS)
            unless scope_options.empty?
              scope(scope_options) do
                public_send(method, resources.pop, options, &block)
              end
              return true
            end

            false
          end

          def apply_action_options(options)
            return options if action_options? options
            options.merge scope_action_options
          end

          def action_options?(options)
            options[:only] || options[:except]
          end

          def scope_action_options
            @scope[:action_options] || {}
          end

          def resource_scope?
            @scope.resource_scope?
          end

          def resource_method_scope?
            @scope.resource_method_scope?
          end

          def nested_scope?
            @scope.nested?
          end

          def with_scope_level(kind) # :doc:
            @scope = @scope.new_level(kind)
            yield
          ensure
            @scope = @scope.parent
          end

          def resource_scope(resource, &block)
            @scope = @scope.new(scope_level_resource: resource)

            controller(resource.resource_scope, &block)
          ensure
            @scope = @scope.parent
          end

          def nested_options
            options = { as: parent_resource.member_name }
            options[:constraints] = {
              parent_resource.nested_param => param_constraint
            } if param_constraint?

            options
          end

          def shallow_nesting_depth
            @scope.find_all { |node|
              node.frame[:scope_level_resource]
            }.count { |node| node.frame[:scope_level_resource].shallow? }
          end

          def param_constraint?
            @scope[:constraints] && @scope[:constraints][parent_resource.param].is_a?(Regexp)
          end

          def param_constraint
            @scope[:constraints][parent_resource.param]
          end

          def canonical_action?(action)
            resource_method_scope? && CANONICAL_ACTIONS.include?(action.to_s)
          end

          def shallow_scope
            scope = { as: @scope[:shallow_prefix],
                      path: @scope[:shallow_path] }
            @scope = @scope.new scope

            yield
          ensure
            @scope = @scope.parent
          end

          def path_for_action(action, path)
            return "#{@scope[:path]}/#{path}" if path

            if canonical_action?(action)
              @scope[:path].to_s
            else
              "#{@scope[:path]}/#{action_path(action)}"
            end
          end

          def action_path(name)
            @scope[:path_names][name.to_sym] || name
          end

          def prefix_name_for_action(as, action)
            if as
              prefix = as
            elsif !canonical_action?(action)
              prefix = action
            end

            if prefix && prefix != "/" && !prefix.empty?
              Mapper.normalize_name prefix.to_s.tr("-", "_")
            end
          end

          def name_for_action(as, action)
            prefix = prefix_name_for_action(as, action)
            name_prefix = @scope[:as]

            if parent_resource
              return nil unless as || action

              collection_name = parent_resource.collection_name
              member_name = parent_resource.member_name
            end

            action_name = @scope.action_name(name_prefix, prefix, collection_name, member_name)
            candidate = action_name.select(&:present?).join("_")

            unless candidate.empty?
              # If a name was not explicitly given, we check if it is valid
              # and return nil in case it isn't. Otherwise, we pass the invalid name
              # forward so the underlying router engine treats it and raises an exception.
              if as.nil?
                candidate unless !candidate.match?(/\A[_a-z]/i) || has_named_route?(candidate)
              else
                candidate
              end
            end
          end

          def set_member_mappings_for_resource # :doc:
            member do
              get :edit if parent_resource.actions.include?(:edit)
              get :show if parent_resource.actions.include?(:show)
              if parent_resource.actions.include?(:update)
                patch :update
                put   :update
              end
              delete :destroy if parent_resource.actions.include?(:destroy)
            end
          end

          def api_only? # :doc:
            @set.api_only?
          end

          def path_scope(path)
            @scope = @scope.new(path: merge_path_scope(@scope[:path], path))
            yield
          ensure
            @scope = @scope.parent
          end

          def map_match(paths, options)
            if (on = options[:on]) && !VALID_ON_OPTIONS.include?(on)
              raise ArgumentError, "Unknown scope #{on.inspect} given to :on"
            end

            if @scope[:to]
              options[:to] ||= @scope[:to]
            end

            if @scope[:controller] && @scope[:action]
              options[:to] ||= "#{@scope[:controller]}##{@scope[:action]}"
            end

            controller = options.delete(:controller) || @scope[:controller]
            option_path = options.delete :path
            to = options.delete :to
            via = Mapping.check_via Array(options.delete(:via) {
              @scope[:via]
            })
            formatted = options.delete(:format) { @scope[:format] }
            anchor = options.delete(:anchor) { true }
            options_constraints = options.delete(:constraints) || {}

            path_types = paths.group_by(&:class)
            (path_types[String] || []).each do |_path|
              route_options = options.dup
              if _path && option_path
                raise ArgumentError, "Ambiguous route definition. Both :path and the route path were specified as strings."
              end
              to = get_to_from_path(_path, to, route_options[:action])
              decomposed_match(_path, controller, route_options, _path, to, via, formatted, anchor, options_constraints)
            end

            (path_types[Symbol] || []).each do |action|
              route_options = options.dup
              decomposed_match(action, controller, route_options, option_path, to, via, formatted, anchor, options_constraints)
            end

            self
          end

          def get_to_from_path(path, to, action)
            return to if to || action

            path_without_format = path.sub(/\(\.:format\)$/, "")
            if using_match_shorthand?(path_without_format)
              path_without_format.delete_prefix("/").sub(%r{/([^/]*)$}, '#\1').tr("-", "_")
            else
              nil
            end
          end

          def using_match_shorthand?(path)
            %r{^/?[-\w]+/[-\w/]+$}.match?(path)
          end

          def decomposed_match(path, controller, options, _path, to, via, formatted, anchor, options_constraints)
            if on = options.delete(:on)
              send(on) { decomposed_match(path, controller, options, _path, to, via, formatted, anchor, options_constraints) }
            else
              case @scope.scope_level
              when :resources
                nested { decomposed_match(path, controller, options, _path, to, via, formatted, anchor, options_constraints) }
              when :resource
                member { decomposed_match(path, controller, options, _path, to, via, formatted, anchor, options_constraints) }
              else
                add_route(path, controller, options, _path, to, via, formatted, anchor, options_constraints)
              end
            end
          end

          def add_route(action, controller, options, _path, to, via, formatted, anchor, options_constraints)
            path = path_for_action(action, _path)
            raise ArgumentError, "path is required" if path.blank?

            action = action.to_s

            default_action = options.delete(:action) || @scope[:action]

            if /^[\w\-\/]+$/.match?(action)
              default_action ||= action.tr("-", "_") unless action.include?("/")
            else
              action = nil
            end

            as = if !options.fetch(:as, true) # if it's set to nil or false
              options.delete(:as)
            else
              name_for_action(options.delete(:as), action)
            end

            path = Mapping.normalize_path URI::DEFAULT_PARSER.escape(path), formatted
            ast = Journey::Parser.parse path

            mapping = Mapping.build(@scope, @set, ast, controller, default_action, to, via, formatted, options_constraints, anchor, options)
            @set.add_route(mapping, as)
          end

          def match_root_route(options)
            args = ["/", { as: :root, via: :get }.merge(options)]
            match(*args)
          end
      end

      # Routing Concerns allow you to declare common routes that can be reused
      # inside others resources and routes.
      #
      #   concern :commentable do
      #     resources :comments
      #   end
      #
      #   concern :image_attachable do
      #     resources :images, only: :index
      #   end
      #
      # These concerns are used in Resources routing:
      #
      #   resources :messages, concerns: [:commentable, :image_attachable]
      #
      # or in a scope or namespace:
      #
      #   namespace :posts do
      #     concerns :commentable
      #   end
      module Concerns
        # Define a routing concern using a name.
        #
        # Concerns may be defined inline, using a block, or handled by
        # another object, by passing that object as the second parameter.
        #
        # The concern object, if supplied, should respond to <tt>call</tt>,
        # which will receive two parameters:
        #
        #   * The current mapper
        #   * A hash of options which the concern object may use
        #
        # Options may also be used by concerns defined in a block by accepting
        # a block parameter. So, using a block, you might do something as
        # simple as limit the actions available on certain resources, passing
        # standard resource options through the concern:
        #
        #   concern :commentable do |options|
        #     resources :comments, options
        #   end
        #
        #   resources :posts, concerns: :commentable
        #   resources :archived_posts do
        #     # Don't allow comments on archived posts
        #     concerns :commentable, only: [:index, :show]
        #   end
        #
        # Or, using a callable object, you might implement something more
        # specific to your application, which would be out of place in your
        # routes file.
        #
        #   # purchasable.rb
        #   class Purchasable
        #     def initialize(defaults = {})
        #       @defaults = defaults
        #     end
        #
        #     def call(mapper, options = {})
        #       options = @defaults.merge(options)
        #       mapper.resources :purchases
        #       mapper.resources :receipts
        #       mapper.resources :returns if options[:returnable]
        #     end
        #   end
        #
        #   # routes.rb
        #   concern :purchasable, Purchasable.new(returnable: true)
        #
        #   resources :toys, concerns: :purchasable
        #   resources :electronics, concerns: :purchasable
        #   resources :pets do
        #     concerns :purchasable, returnable: false
        #   end
        #
        # Any routing helpers can be used inside a concern. If using a
        # callable, they're accessible from the Mapper that's passed to
        # <tt>call</tt>.
        def concern(name, callable = nil, &block)
          callable ||= lambda { |mapper, options| mapper.instance_exec(options, &block) }
          @concerns[name] = callable
        end

        # Use the named concerns
        #
        #   resources :posts do
        #     concerns :commentable
        #   end
        #
        # Concerns also work in any routes helper that you want to use:
        #
        #   namespace :posts do
        #     concerns :commentable
        #   end
        def concerns(*args)
          options = args.extract_options!
          args.flatten.each do |name|
            if concern = @concerns[name]
              concern.call(self, options)
            else
              raise ArgumentError, "No concern named #{name} was found!"
            end
          end
        end
      end

      module CustomUrls
        # Define custom URL helpers that will be added to the application's
        # routes. This allows you to override and/or replace the default behavior
        # of routing helpers, e.g:
        #
        #   direct :homepage do
        #     "https://rubyonrails.org"
        #   end
        #
        #   direct :commentable do |model|
        #     [ model, anchor: model.dom_id ]
        #   end
        #
        #   direct :main do
        #     { controller: "pages", action: "index", subdomain: "www" }
        #   end
        #
        # The return value from the block passed to +direct+ must be a valid set of
        # arguments for +url_for+ which will actually build the URL string. This can
        # be one of the following:
        #
        # * A string, which is treated as a generated URL
        # * A hash, e.g. <tt>{ controller: "pages", action: "index" }</tt>
        # * An array, which is passed to +polymorphic_url+
        # * An Active Model instance
        # * An Active Model class
        #
        # NOTE: Other URL helpers can be called in the block but be careful not to invoke
        # your custom URL helper again otherwise it will result in a stack overflow error.
        #
        # You can also specify default options that will be passed through to
        # your URL helper definition, e.g:
        #
        #   direct :browse, page: 1, size: 10 do |options|
        #     [ :products, options.merge(params.permit(:page, :size).to_h.symbolize_keys) ]
        #   end
        #
        # In this instance the +params+ object comes from the context in which the
        # block is executed, e.g. generating a URL inside a controller action or a view.
        # If the block is executed where there isn't a +params+ object such as this:
        #
        #   Rails.application.routes.url_helpers.browse_path
        #
        # then it will raise a +NameError+. Because of this you need to be aware of the
        # context in which you will use your custom URL helper when defining it.
        #
        # NOTE: The +direct+ method can't be used inside of a scope block such as
        # +namespace+ or +scope+ and will raise an error if it detects that it is.
        def direct(name, options = {}, &block)
          unless @scope.root?
            raise RuntimeError, "The direct method can't be used inside a routes scope block"
          end

          @set.add_url_helper(name, options, &block)
        end

        # Define custom polymorphic mappings of models to URLs. This alters the
        # behavior of +polymorphic_url+ and consequently the behavior of
        # +link_to+ and +form_for+ when passed a model instance, e.g:
        #
        #   resource :basket
        #
        #   resolve "Basket" do
        #     [:basket]
        #   end
        #
        # This will now generate "/basket" when a +Basket+ instance is passed to
        # +link_to+ or +form_for+ instead of the standard "/baskets/:id".
        #
        # NOTE: This custom behavior only applies to simple polymorphic URLs where
        # a single model instance is passed and not more complicated forms, e.g:
        #
        #   # config/routes.rb
        #   resource :profile
        #   namespace :admin do
        #     resources :users
        #   end
        #
        #   resolve("User") { [:profile] }
        #
        #   # app/views/application/_menu.html.erb
        #   link_to "Profile", @current_user
        #   link_to "Profile", [:admin, @current_user]
        #
        # The first +link_to+ will generate "/profile" but the second will generate
        # the standard polymorphic URL of "/admin/users/1".
        #
        # You can pass options to a polymorphic mapping - the arity for the block
        # needs to be two as the instance is passed as the first argument, e.g:
        #
        #   resolve "Basket", anchor: "items" do |basket, options|
        #     [:basket, options]
        #   end
        #
        # This generates the URL "/basket#items" because when the last item in an
        # array passed to +polymorphic_url+ is a hash then it's treated as options
        # to the URL helper that gets called.
        #
        # NOTE: The +resolve+ method can't be used inside of a scope block such as
        # +namespace+ or +scope+ and will raise an error if it detects that it is.
        def resolve(*args, &block)
          unless @scope.root?
            raise RuntimeError, "The resolve method can't be used inside a routes scope block"
          end

          options = args.extract_options!
          args = args.flatten(1)

          args.each do |klass|
            @set.add_polymorphic_mapping(klass, options, &block)
          end
        end
      end

      class Scope # :nodoc:
        OPTIONS = [:path, :shallow_path, :as, :shallow_prefix, :module,
                   :controller, :action, :path_names, :constraints,
                   :shallow, :blocks, :defaults, :via, :format, :options, :to]

        RESOURCE_SCOPES = [:resource, :resources]
        RESOURCE_METHOD_SCOPES = [:collection, :member, :new]

        attr_reader :parent, :scope_level

        def initialize(hash, parent = NULL, scope_level = nil)
          @hash = hash
          @parent = parent
          @scope_level = scope_level
        end

        def nested?
          scope_level == :nested
        end

        def null?
          @hash.nil? && @parent.nil?
        end

        def root?
          @parent.null?
        end

        def resources?
          scope_level == :resources
        end

        def resource_method_scope?
          RESOURCE_METHOD_SCOPES.include? scope_level
        end

        def action_name(name_prefix, prefix, collection_name, member_name)
          case scope_level
          when :nested
            [name_prefix, prefix]
          when :collection
            [prefix, name_prefix, collection_name]
          when :new
            [prefix, :new, name_prefix, member_name]
          when :member
            [prefix, name_prefix, member_name]
          when :root
            [name_prefix, collection_name, prefix]
          else
            [name_prefix, member_name, prefix]
          end
        end

        def resource_scope?
          RESOURCE_SCOPES.include? scope_level
        end

        def options
          OPTIONS
        end

        def new(hash)
          self.class.new hash, self, scope_level
        end

        def new_level(level)
          self.class.new(frame, self, level)
        end

        def [](key)
          scope = find { |node| node.frame.key? key }
          scope && scope.frame[key]
        end

        include Enumerable

        def each
          node = self
          until node.equal? NULL
            yield node
            node = node.parent
          end
        end

        def frame; @hash; end

        NULL = Scope.new(nil, nil)
      end

      def initialize(set) # :nodoc:
        @set = set
        @draw_paths = set.draw_paths
        @scope = Scope.new(path_names: @set.resources_path_names)
        @concerns = {}
      end

      include Base
      include HttpHelpers
      include Redirection
      include Scoping
      include Concerns
      include Resources
      include CustomUrls
    end
  end
end


// --- rb_form_helper.rb ---
# frozen_string_literal: true

require "cgi"
require "action_view/helpers/date_helper"
require "action_view/helpers/url_helper"
require "action_view/helpers/form_tag_helper"
require "action_view/helpers/active_model_helper"
require "action_view/model_naming"
require "action_view/record_identifier"
require "active_support/core_ext/module/attribute_accessors"
require "active_support/core_ext/hash/slice"
require "active_support/core_ext/string/output_safety"
require "active_support/core_ext/string/inflections"

module ActionView
  module Helpers # :nodoc:
    # = Action View Form \Helpers
    #
    # Form helpers are designed to make working with resources much easier
    # compared to using vanilla HTML.
    #
    # Typically, a form designed to create or update a resource reflects the
    # identity of the resource in several ways: (i) the URL that the form is
    # sent to (the form element's +action+ attribute) should result in a request
    # being routed to the appropriate controller action (with the appropriate <tt>:id</tt>
    # parameter in the case of an existing resource), (ii) input fields should
    # be named in such a way that in the controller their values appear in the
    # appropriate places within the +params+ hash, and (iii) for an existing record,
    # when the form is initially displayed, input fields corresponding to attributes
    # of the resource should show the current values of those attributes.
    #
    # In \Rails, this is usually achieved by creating the form using +form_for+ and
    # a number of related helper methods. +form_for+ generates an appropriate <tt>form</tt>
    # tag and yields a form builder object that knows the model the form is about.
    # Input fields are created by calling methods defined on the form builder, which
    # means they are able to generate the appropriate names and default values
    # corresponding to the model attributes, as well as convenient IDs, etc.
    # Conventions in the generated field names allow controllers to receive form data
    # nicely structured in +params+ with no effort on your side.
    #
    # For example, to create a new person you typically set up a new instance of
    # +Person+ in the <tt>PeopleController#new</tt> action, <tt>@person</tt>, and
    # in the view template pass that object to +form_for+:
    #
    #   <%= form_for @person do |f| %>
    #     <%= f.label :first_name %>:
    #     <%= f.text_field :first_name %><br />
    #
    #     <%= f.label :last_name %>:
    #     <%= f.text_field :last_name %><br />
    #
    #     <%= f.submit %>
    #   <% end %>
    #
    # The HTML generated for this would be (modulus formatting):
    #
    #   <form action="/people" class="new_person" id="new_person" method="post">
    #     <input name="authenticity_token" type="hidden" value="NrOp5bsjoLRuK8IW5+dQEYjKGUJDe7TQoZVvq95Wteg=" />
    #     <label for="person_first_name">First name</label>:
    #     <input id="person_first_name" name="person[first_name]" type="text" /><br />
    #
    #     <label for="person_last_name">Last name</label>:
    #     <input id="person_last_name" name="person[last_name]" type="text" /><br />
    #
    #     <input name="commit" type="submit" value="Create Person" />
    #   </form>
    #
    # As you see, the HTML reflects knowledge about the resource in several spots,
    # like the path the form should be submitted to, or the names of the input fields.
    #
    # In particular, thanks to the conventions followed in the generated field names, the
    # controller gets a nested hash <tt>params[:person]</tt> with the person attributes
    # set in the form. That hash is ready to be passed to <tt>Person.new</tt>:
    #
    #   @person = Person.new(params[:person])
    #   if @person.save
    #     # success
    #   else
    #     # error handling
    #   end
    #
    # Interestingly, the exact same view code in the previous example can be used to edit
    # a person. If <tt>@person</tt> is an existing record with name "John Smith" and ID 256,
    # the code above as is would yield instead:
    #
    #   <form action="/people/256" class="edit_person" id="edit_person_256" method="post">
    #     <input name="_method" type="hidden" value="patch" />
    #     <input name="authenticity_token" type="hidden" value="NrOp5bsjoLRuK8IW5+dQEYjKGUJDe7TQoZVvq95Wteg=" />
    #     <label for="person_first_name">First name</label>:
    #     <input id="person_first_name" name="person[first_name]" type="text" value="John" /><br />
    #
    #     <label for="person_last_name">Last name</label>:
    #     <input id="person_last_name" name="person[last_name]" type="text" value="Smith" /><br />
    #
    #     <input name="commit" type="submit" value="Update Person" />
    #   </form>
    #
    # Note that the endpoint, default values, and submit button label are tailored for <tt>@person</tt>.
    # That works that way because the involved helpers know whether the resource is a new record or not,
    # and generate HTML accordingly.
    #
    # The controller would receive the form data again in <tt>params[:person]</tt>, ready to be
    # passed to <tt>Person#update</tt>:
    #
    #   if @person.update(params[:person])
    #     # success
    #   else
    #     # error handling
    #   end
    #
    # That's how you typically work with resources.
    module FormHelper
      extend ActiveSupport::Concern

      include FormTagHelper
      include UrlHelper
      include ModelNaming
      include RecordIdentifier

      attr_internal :default_form_builder

      # Creates a form that allows the user to create or update the attributes
      # of a specific model object.
      #
      # The method can be used in several slightly different ways, depending on
      # how much you wish to rely on \Rails to infer automatically from the model
      # how the form should be constructed. For a generic model object, a form
      # can be created by passing +form_for+ a string or symbol representing
      # the object we are concerned with:
      #
      #   <%= form_for :person do |f| %>
      #     First name: <%= f.text_field :first_name %><br />
      #     Last name : <%= f.text_field :last_name %><br />
      #     Biography : <%= f.text_area :biography %><br />
      #     Admin?    : <%= f.check_box :admin %><br />
      #     <%= f.submit %>
      #   <% end %>
      #
      # The variable +f+ yielded to the block is a FormBuilder object that
      # incorporates the knowledge about the model object represented by
      # <tt>:person</tt> passed to +form_for+. Methods defined on the FormBuilder
      # are used to generate fields bound to this model. Thus, for example,
      #
      #   <%= f.text_field :first_name %>
      #
      # will get expanded to
      #
      #   <%= text_field :person, :first_name %>
      #
      # which results in an HTML <tt><input></tt> tag whose +name+ attribute is
      # <tt>person[first_name]</tt>. This means that when the form is submitted,
      # the value entered by the user will be available in the controller as
      # <tt>params[:person][:first_name]</tt>.
      #
      # For fields generated in this way using the FormBuilder,
      # if <tt>:person</tt> also happens to be the name of an instance variable
      # <tt>@person</tt>, the default value of the field shown when the form is
      # initially displayed (e.g. in the situation where you are editing an
      # existing record) will be the value of the corresponding attribute of
      # <tt>@person</tt>.
      #
      # The rightmost argument to +form_for+ is an
      # optional hash of options -
      #
      # * <tt>:url</tt> - The URL the form is to be submitted to. This may be
      #   represented in the same way as values passed to +url_for+ or +link_to+.
      #   So for example you may use a named route directly. When the model is
      #   represented by a string or symbol, as in the example above, if the
      #   <tt>:url</tt> option is not specified, by default the form will be
      #   sent back to the current URL (We will describe below an alternative
      #   resource-oriented usage of +form_for+ in which the URL does not need
      #   to be specified explicitly).
      # * <tt>:namespace</tt> - A namespace for your form to ensure uniqueness of
      #   id attributes on form elements. The namespace attribute will be prefixed
      #   with underscore on the generated HTML id.
      # * <tt>:method</tt> - The method to use when submitting the form, usually
      #   either "get" or "post". If "patch", "put", "delete", or another verb
      #   is used, a hidden input with name <tt>_method</tt> is added to
      #   simulate the verb over post.
      # * <tt>:authenticity_token</tt> - Authenticity token to use in the form.
      #   Use only if you need to pass custom authenticity token string, or to
      #   not add authenticity_token field at all (by passing <tt>false</tt>).
      #   Remote forms may omit the embedded authenticity token by setting
      #   <tt>config.action_view.embed_authenticity_token_in_remote_forms = false</tt>.
      #   This is helpful when you're fragment-caching the form. Remote forms
      #   get the authenticity token from the <tt>meta</tt> tag, so embedding is
      #   unnecessary unless you support browsers without JavaScript.
      # * <tt>:remote</tt> - If set to true, will allow the Unobtrusive
      #   JavaScript drivers to control the submit behavior.
      # * <tt>:enforce_utf8</tt> - If set to false, a hidden input with name
      #   utf8 is not output.
      # * <tt>:html</tt> - Optional HTML attributes for the form tag.
      #
      # Also note that +form_for+ doesn't create an exclusive scope. It's still
      # possible to use both the stand-alone FormHelper methods and methods
      # from FormTagHelper. For example:
      #
      #   <%= form_for :person do |f| %>
      #     First name: <%= f.text_field :first_name %>
      #     Last name : <%= f.text_field :last_name %>
      #     Biography : <%= text_area :person, :biography %>
      #     Admin?    : <%= check_box_tag "person[admin]", "1", @person.company.admin? %>
      #     <%= f.submit %>
      #   <% end %>
      #
      # This also works for the methods in FormOptionsHelper and DateHelper that
      # are designed to work with an object as base, like
      # FormOptionsHelper#collection_select and DateHelper#datetime_select.
      #
      # === #form_for with a model object
      #
      # In the examples above, the object to be created or edited was
      # represented by a symbol passed to +form_for+, and we noted that
      # a string can also be used equivalently. It is also possible, however,
      # to pass a model object itself to +form_for+. For example, if <tt>@post</tt>
      # is an existing record you wish to edit, you can create the form using
      #
      #   <%= form_for @post do |f| %>
      #     ...
      #   <% end %>
      #
      # This behaves in almost the same way as outlined previously, with a
      # couple of small exceptions. First, the prefix used to name the input
      # elements within the form (hence the key that denotes them in the +params+
      # hash) is actually derived from the object's _class_, e.g. <tt>params[:post]</tt>
      # if the object's class is +Post+. However, this can be overwritten using
      # the <tt>:as</tt> option, e.g. -
      #
      #   <%= form_for(@person, as: :client) do |f| %>
      #     ...
      #   <% end %>
      #
      # would result in <tt>params[:client]</tt>.
      #
      # Secondly, the field values shown when the form is initially displayed
      # are taken from the attributes of the object passed to +form_for+,
      # regardless of whether the object is an instance
      # variable. So, for example, if we had a _local_ variable +post+
      # representing an existing record,
      #
      #   <%= form_for post do |f| %>
      #     ...
      #   <% end %>
      #
      # would produce a form with fields whose initial state reflect the current
      # values of the attributes of +post+.
      #
      # === Resource-oriented style
      #
      # In the examples just shown, although not indicated explicitly, we still
      # need to use the <tt>:url</tt> option in order to specify where the
      # form is going to be sent. However, further simplification is possible
      # if the record passed to +form_for+ is a _resource_, i.e. it corresponds
      # to a set of RESTful routes, e.g. defined using the +resources+ method
      # in <tt>config/routes.rb</tt>. In this case \Rails will simply infer the
      # appropriate URL from the record itself. For example,
      #
      #   <%= form_for @post do |f| %>
      #     ...
      #   <% end %>
      #
      # is then equivalent to something like:
      #
      #   <%= form_for @post, as: :post, url: post_path(@post), method: :patch, html: { class: "edit_post", id: "edit_post_45" } do |f| %>
      #     ...
      #   <% end %>
      #
      # And for a new record
      #
      #   <%= form_for(Post.new) do |f| %>
      #     ...
      #   <% end %>
      #
      # is equivalent to something like:
      #
      #   <%= form_for @post, as: :post, url: posts_path, html: { class: "new_post", id: "new_post" } do |f| %>
      #     ...
      #   <% end %>
      #
      # However you can still overwrite individual conventions, such as:
      #
      #   <%= form_for(@post, url: super_posts_path) do |f| %>
      #     ...
      #   <% end %>
      #
      # You can omit the <tt>action</tt> attribute by passing <tt>url: false</tt>:
      #
      #   <%= form_for(@post, url: false) do |f| %>
      #     ...
      #   <% end %>
      #
      # You can also set the answer format, like this:
      #
      #   <%= form_for(@post, format: :json) do |f| %>
      #     ...
      #   <% end %>
      #
      # For namespaced routes, like +admin_post_url+:
      #
      #   <%= form_for([:admin, @post]) do |f| %>
      #    ...
      #   <% end %>
      #
      # If your resource has associations defined, for example, you want to add comments
      # to the document given that the routes are set correctly:
      #
      #   <%= form_for([@document, @comment]) do |f| %>
      #    ...
      #   <% end %>
      #
      # Where <tt>@document = Document.find(params[:id])</tt> and
      # <tt>@comment = Comment.new</tt>.
      #
      # === Setting the method
      #
      # You can force the form to use the full array of HTTP verbs by setting
      #
      #    method: (:get|:post|:patch|:put|:delete)
      #
      # in the options hash. If the verb is not GET or POST, which are natively
      # supported by HTML forms, the form will be set to POST and a hidden input
      # called _method will carry the intended verb for the server to interpret.
      #
      # === Unobtrusive JavaScript
      #
      # Specifying:
      #
      #    remote: true
      #
      # in the options hash creates a form that will allow the unobtrusive JavaScript drivers to modify its
      # behavior. The form submission will work just like a regular submission as viewed by the receiving
      # side (all elements available in <tt>params</tt>).
      #
      # Example:
      #
      #   <%= form_for(@post, remote: true) do |f| %>
      #     ...
      #   <% end %>
      #
      # The HTML generated for this would be:
      #
      #   <form action='http://www.example.com' method='post' data-remote='true'>
      #     <input name='_method' type='hidden' value='patch' />
      #     ...
      #   </form>
      #
      # === Setting HTML options
      #
      # You can set data attributes directly by passing in a data hash, but all other HTML options must be wrapped in
      # the HTML key. Example:
      #
      #   <%= form_for(@post, data: { behavior: "autosave" }, html: { name: "go" }) do |f| %>
      #     ...
      #   <% end %>
      #
      # The HTML generated for this would be:
      #
      #   <form action='http://www.example.com' method='post' data-behavior='autosave' name='go'>
      #     <input name='_method' type='hidden' value='patch' />
      #     ...
      #   </form>
      #
      # === Removing hidden model id's
      #
      # The form_for method automatically includes the model id as a hidden field in the form.
      # This is used to maintain the correlation between the form data and its associated model.
      # Some ORM systems do not use IDs on nested models so in this case you want to be able
      # to disable the hidden id.
      #
      # In the following example the Post model has many Comments stored within it in a NoSQL database,
      # thus there is no primary key for comments.
      #
      # Example:
      #
      #   <%= form_for(@post) do |f| %>
      #     <%= f.fields_for(:comments, include_id: false) do |cf| %>
      #       ...
      #     <% end %>
      #   <% end %>
      #
      # === Customized form builders
      #
      # You can also build forms using a customized FormBuilder class. Subclass
      # FormBuilder and override or define some more helpers, then use your
      # custom builder. For example, let's say you made a helper to
      # automatically add labels to form inputs.
      #
      #   <%= form_for @person, url: { action: "create" }, builder: LabellingFormBuilder do |f| %>
      #     <%= f.text_field :first_name %>
      #     <%= f.text_field :last_name %>
      #     <%= f.text_area :biography %>
      #     <%= f.check_box :admin %>
      #     <%= f.submit %>
      #   <% end %>
      #
      # In this case, if you use this:
      #
      #   <%= render f %>
      #
      # The rendered template is <tt>people/_labelling_form</tt> and the local
      # variable referencing the form builder is called
      # <tt>labelling_form</tt>.
      #
      # The custom FormBuilder class is automatically merged with the options
      # of a nested fields_for call, unless it's explicitly set.
      #
      # In many cases you will want to wrap the above in another helper, so you
      # could do something like the following:
      #
      #   def labelled_form_for(record_or_name_or_array, *args, &block)
      #     options = args.extract_options!
      #     form_for(record_or_name_or_array, *(args << options.merge(builder: LabellingFormBuilder)), &block)
      #   end
      #
      # If you don't need to attach a form to a model instance, then check out
      # FormTagHelper#form_tag.
      #
      # === Form to external resources
      #
      # When you build forms to external resources sometimes you need to set an authenticity token or just render a form
      # without it, for example when you submit data to a payment gateway number and types of fields could be limited.
      #
      # To set an authenticity token you need to pass an <tt>:authenticity_token</tt> parameter
      #
      #   <%= form_for @invoice, url: external_url, authenticity_token: 'external_token' do |f| %>
      #     ...
      #   <% end %>
      #
      # If you don't want to an authenticity token field be rendered at all just pass <tt>false</tt>:
      #
      #   <%= form_for @invoice, url: external_url, authenticity_token: false do |f| %>
      #     ...
      #   <% end %>
      def form_for(record, options = {}, &block)
        raise ArgumentError, "Missing block" unless block_given?

        case record
        when String, Symbol
          model       = nil
          object_name = record
        else
          model       = record
          object      = _object_for_form_builder(record)
          raise ArgumentError, "First argument in form cannot contain nil or be empty" unless object
          object_name = options[:as] || model_name_from_record_or_class(object).param_key
          apply_form_for_options!(object, options)
        end

        remote = options.delete(:remote)

        if remote && !embed_authenticity_token_in_remote_forms && options[:authenticity_token].blank?
          options[:authenticity_token] = false
        end

        options[:model]                               = model
        options[:scope]                               = object_name
        options[:local]                               = !remote
        options[:skip_default_ids]                    = false
        options[:allow_method_names_outside_object]   = options.fetch(:allow_method_names_outside_object, false)

        form_with(**options, &block)
      end

      def apply_form_for_options!(object, options) # :nodoc:
        object = convert_to_model(object)

        as = options[:as]
        namespace = options[:namespace]
        action = object.respond_to?(:persisted?) && object.persisted? ? :edit : :new
        options[:html] ||= {}
        options[:html].reverse_merge!(
          class:  as ? "#{action}_#{as}" : dom_class(object, action),
          id:     (as ? [namespace, action, as] : [namespace, dom_id(object, action)]).compact.join("_").presence,
        )
      end
      private :apply_form_for_options!

      mattr_accessor :form_with_generates_remote_forms, default: true

      mattr_accessor :form_with_generates_ids, default: false

      mattr_accessor :multiple_file_field_include_hidden, default: false

      # Creates a form tag based on mixing URLs, scopes, or models.
      #
      #   # Using just a URL:
      #   <%= form_with url: posts_path do |form| %>
      #     <%= form.text_field :title %>
      #   <% end %>
      #   # =>
      #   <form action="/posts" method="post">
      #     <input type="text" name="title">
      #   </form>
      #
      #   # With an intentionally empty URL:
      #   <%= form_with url: false do |form| %>
      #     <%= form.text_field :title %>
      #   <% end %>
      #   # =>
      #   <form method="post">
      #     <input type="text" name="title">
      #   </form>
      #
      #   # Adding a scope prefixes the input field names:
      #   <%= form_with scope: :post, url: posts_path do |form| %>
      #     <%= form.text_field :title %>
      #   <% end %>
      #   # =>
      #   <form action="/posts" method="post">
      #     <input type="text" name="post[title]">
      #   </form>
      #
      #   # Using a model infers both the URL and scope:
      #   <%= form_with model: Post.new do |form| %>
      #     <%= form.text_field :title %>
      #   <% end %>
      #   # =>
      #   <form action="/posts" method="post">
      #     <input type="text" name="post[title]">
      #   </form>
      #
      #   # An existing model makes an update form and fills out field values:
      #   <%= form_with model: Post.first do |form| %>
      #     <%= form.text_field :title %>
      #   <% end %>
      #   # =>
      #   <form action="/posts/1" method="post">
      #     <input type="hidden" name="_method" value="patch">
      #     <input type="text" name="post[title]" value="<the title of the post>">
      #   </form>
      #
      #   # Though the fields don't have to correspond to model attributes:
      #   <%= form_with model: Cat.new do |form| %>
      #     <%= form.text_field :cats_dont_have_gills %>
      #     <%= form.text_field :but_in_forms_they_can %>
      #   <% end %>
      #   # =>
      #   <form action="/cats" method="post">
      #     <input type="text" name="cat[cats_dont_have_gills]">
      #     <input type="text" name="cat[but_in_forms_they_can]">
      #   </form>
      #
      # The parameters in the forms are accessible in controllers according to
      # their name nesting. So inputs named +title+ and <tt>post[title]</tt> are
      # accessible as <tt>params[:title]</tt> and <tt>params[:post][:title]</tt>
      # respectively.
      #
      # For ease of comparison the examples above left out the submit button,
      # as well as the auto generated hidden fields that enable UTF-8 support
      # and adds an authenticity token needed for cross site request forgery
      # protection.
      #
      # === Resource-oriented style
      #
      # In many of the examples just shown, the +:model+ passed to +form_with+
      # is a _resource_. It corresponds to a set of RESTful routes, most likely
      # defined via +resources+ in <tt>config/routes.rb</tt>.
      #
      # So when passing such a model record, \Rails infers the URL and method.
      #
      #   <%= form_with model: @post do |form| %>
      #     ...
      #   <% end %>
      #
      # is then equivalent to something like:
      #
      #   <%= form_with scope: :post, url: post_path(@post), method: :patch do |form| %>
      #     ...
      #   <% end %>
      #
      # And for a new record
      #
      #   <%= form_with model: Post.new do |form| %>
      #     ...
      #   <% end %>
      #
      # is equivalent to something like:
      #
      #   <%= form_with scope: :post, url: posts_path do |form| %>
      #     ...
      #   <% end %>
      #
      # ==== +form_with+ options
      #
      # * <tt>:url</tt> - The URL the form submits to. Akin to values passed to
      #   +url_for+ or +link_to+. For example, you may use a named route
      #   directly. When a <tt>:scope</tt> is passed without a <tt>:url</tt> the
      #   form just submits to the current URL.
      # * <tt>:method</tt> - The method to use when submitting the form, usually
      #   either "get" or "post". If "patch", "put", "delete", or another verb
      #   is used, a hidden input named <tt>_method</tt> is added to
      #   simulate the verb over post.
      # * <tt>:format</tt> - The format of the route the form submits to.
      #   Useful when submitting to another resource type, like <tt>:json</tt>.
      #   Skipped if a <tt>:url</tt> is passed.
      # * <tt>:scope</tt> - The scope to prefix input field names with and
      #   thereby how the submitted parameters are grouped in controllers.
      # * <tt>:namespace</tt> - A namespace for your form to ensure uniqueness of
      #   id attributes on form elements. The namespace attribute will be prefixed
      #   with underscore on the generated HTML id.
      # * <tt>:model</tt> - A model object to infer the <tt>:url</tt> and
      #   <tt>:scope</tt> by, plus fill out input field values.
      #   So if a +title+ attribute is set to "Ahoy!" then a +title+ input
      #   field's value would be "Ahoy!".
      #   If the model is a new record a create form is generated, if an
      #   existing record, however, an update form is generated.
      #   Pass <tt>:scope</tt> or <tt>:url</tt> to override the defaults.
      #   E.g. turn <tt>params[:post]</tt> into <tt>params[:article]</tt>.
      # * <tt>:authenticity_token</tt> - Authenticity token to use in the form.
      #   Override with a custom authenticity token or pass <tt>false</tt> to
      #   skip the authenticity token field altogether.
      #   Useful when submitting to an external resource like a payment gateway
      #   that might limit the valid fields.
      #   Remote forms may omit the embedded authenticity token by setting
      #   <tt>config.action_view.embed_authenticity_token_in_remote_forms = false</tt>.
      #   This is helpful when fragment-caching the form. Remote forms
      #   get the authenticity token from the <tt>meta</tt> tag, so embedding is
      #   unnecessary unless you support browsers without JavaScript.
      # * <tt>:local</tt> - Whether to use standard HTTP form submission.
      #   When set to <tt>true</tt>, the form is submitted via standard HTTP.
      #   When set to <tt>false</tt>, the form is submitted as a "remote form", which
      #   is handled by \Rails UJS as an XHR. When unspecified, the behavior is derived
      #   from <tt>config.action_view.form_with_generates_remote_forms</tt> where the
      #   config's value is actually the inverse of what <tt>local</tt>'s value would be.
      #   As of \Rails 6.1, that configuration option defaults to <tt>false</tt>
      #   (which has the equivalent effect of passing <tt>local: true</tt>).
      #   In previous versions of \Rails, that configuration option defaults to
      #   <tt>true</tt> (the equivalent of passing <tt>local: false</tt>).
      # * <tt>:skip_enforcing_utf8</tt> - If set to true, a hidden input with name
      #   utf8 is not output.
      # * <tt>:builder</tt> - Override the object used to build the form.
      # * <tt>:id</tt> - Optional HTML id attribute.
      # * <tt>:class</tt> - Optional HTML class attribute.
      # * <tt>:data</tt> - Optional HTML data attributes.
      # * <tt>:html</tt> - Other optional HTML attributes for the form tag.
      #
      # === Examples
      #
      # When not passing a block, +form_with+ just generates an opening form tag.
      #
      #   <%= form_with(model: @post, url: super_posts_path) %>
      #   <%= form_with(model: @post, scope: :article) %>
      #   <%= form_with(model: @post, format: :json) %>
      #   <%= form_with(model: @post, authenticity_token: false) %> # Disables the token.
      #
      # For namespaced routes, like +admin_post_url+:
      #
      #   <%= form_with(model: [ :admin, @post ]) do |form| %>
      #     ...
      #   <% end %>
      #
      # If your resource has associations defined, for example, you want to add comments
      # to the document given that the routes are set correctly:
      #
      #   <%= form_with(model: [ @document, Comment.new ]) do |form| %>
      #     ...
      #   <% end %>
      #
      # Where <tt>@document = Document.find(params[:id])</tt>.
      #
      # === Mixing with other form helpers
      #
      # While +form_with+ uses a FormBuilder object it's possible to mix and
      # match the stand-alone FormHelper methods and methods
      # from FormTagHelper:
      #
      #   <%= form_with scope: :person do |form| %>
      #     <%= form.text_field :first_name %>
      #     <%= form.text_field :last_name %>
      #
      #     <%= text_area :person, :biography %>
      #     <%= check_box_tag "person[admin]", "1", @person.company.admin? %>
      #
      #     <%= form.submit %>
      #   <% end %>
      #
      # Same goes for the methods in FormOptionsHelper and DateHelper designed
      # to work with an object as a base, like
      # FormOptionsHelper#collection_select and DateHelper#datetime_select.
      #
      # === Setting the method
      #
      # You can force the form to use the full array of HTTP verbs by setting
      #
      #    method: (:get|:post|:patch|:put|:delete)
      #
      # in the options hash. If the verb is not GET or POST, which are natively
      # supported by HTML forms, the form will be set to POST and a hidden input
      # called _method will carry the intended verb for the server to interpret.
      #
      # === Setting HTML options
      #
      # You can set data attributes directly in a data hash, but HTML options
      # besides id and class must be wrapped in an HTML key:
      #
      #   <%= form_with(model: @post, data: { behavior: "autosave" }, html: { name: "go" }) do |form| %>
      #     ...
      #   <% end %>
      #
      # generates
      #
      #   <form action="/posts/123" method="post" data-behavior="autosave" name="go">
      #     <input name="_method" type="hidden" value="patch" />
      #     ...
      #   </form>
      #
      # === Removing hidden model id's
      #
      # The +form_with+ method automatically includes the model id as a hidden field in the form.
      # This is used to maintain the correlation between the form data and its associated model.
      # Some ORM systems do not use IDs on nested models so in this case you want to be able
      # to disable the hidden id.
      #
      # In the following example the Post model has many Comments stored within it in a NoSQL database,
      # thus there is no primary key for comments.
      #
      #   <%= form_with(model: @post) do |form| %>
      #     <%= form.fields(:comments, skip_id: true) do |fields| %>
      #       ...
      #     <% end %>
      #   <% end %>
      #
      # === Customized form builders
      #
      # You can also build forms using a customized FormBuilder class. Subclass
      # FormBuilder and override or define some more helpers, then use your
      # custom builder. For example, let's say you made a helper to
      # automatically add labels to form inputs.
      #
      #   <%= form_with model: @person, url: { action: "create" }, builder: LabellingFormBuilder do |form| %>
      #     <%= form.text_field :first_name %>
      #     <%= form.text_field :last_name %>
      #     <%= form.text_area :biography %>
      #     <%= form.check_box :admin %>
      #     <%= form.submit %>
      #   <% end %>
      #
      # In this case, if you use:
      #
      #   <%= render form %>
      #
      # The rendered template is <tt>people/_labelling_form</tt> and the local
      # variable referencing the form builder is called
      # <tt>labelling_form</tt>.
      #
      # The custom FormBuilder class is automatically merged with the options
      # of a nested +fields+ call, unless it's explicitly set.
      #
      # In many cases you will want to wrap the above in another helper, so you
      # could do something like the following:
      #
      #   def labelled_form_with(**options, &block)
      #     form_with(**options.merge(builder: LabellingFormBuilder), &block)
      #   end
      def form_with(model: nil, scope: nil, url: nil, format: nil, **options, &block)
        options = { allow_method_names_outside_object: true, skip_default_ids: !form_with_generates_ids }.merge!(options)

        if model
          if url != false
            url ||= if format.nil?
              polymorphic_path(model, {})
            else
              polymorphic_path(model, format: format)
            end
          end

          model   = convert_to_model(_object_for_form_builder(model))
          scope ||= model_name_from_record_or_class(model).param_key
        end

        if block_given?
          builder = instantiate_builder(scope, model, options)
          output  = capture(builder, &block)
          options[:multipart] ||= builder.multipart?

          html_options = html_options_for_form_with(url, model, **options)
          form_tag_with_body(html_options, output)
        else
          html_options = html_options_for_form_with(url, model, **options)
          form_tag_html(html_options)
        end
      end

      # Creates a scope around a specific model object like form_for, but
      # doesn't create the form tags themselves. This makes fields_for suitable
      # for specifying additional model objects in the same form.
      #
      # Although the usage and purpose of +fields_for+ is similar to +form_for+'s,
      # its method signature is slightly different. Like +form_for+, it yields
      # a FormBuilder object associated with a particular model object to a block,
      # and within the block allows methods to be called on the builder to
      # generate fields associated with the model object. Fields may reflect
      # a model object in two ways - how they are named (hence how submitted
      # values appear within the +params+ hash in the controller) and what
      # default values are shown when the form the fields appear in is first
      # displayed. In order for both of these features to be specified independently,
      # both an object name (represented by either a symbol or string) and the
      # object itself can be passed to the method separately -
      #
      #   <%= form_for @person do |person_form| %>
      #     First name: <%= person_form.text_field :first_name %>
      #     Last name : <%= person_form.text_field :last_name %>
      #
      #     <%= fields_for :permission, @person.permission do |permission_fields| %>
      #       Admin?  : <%= permission_fields.check_box :admin %>
      #     <% end %>
      #
      #     <%= person_form.submit %>
      #   <% end %>
      #
      # In this case, the checkbox field will be represented by an HTML +input+
      # tag with the +name+ attribute <tt>permission[admin]</tt>, and the submitted
      # value will appear in the controller as <tt>params[:permission][:admin]</tt>.
      # If <tt>@person.permission</tt> is an existing record with an attribute
      # +admin+, the initial state of the checkbox when first displayed will
      # reflect the value of <tt>@person.permission.admin</tt>.
      #
      # Often this can be simplified by passing just the name of the model
      # object to +fields_for+ -
      #
      #   <%= fields_for :permission do |permission_fields| %>
      #     Admin?: <%= permission_fields.check_box :admin %>
      #   <% end %>
      #
      # ...in which case, if <tt>:permission</tt> also happens to be the name of an
      # instance variable <tt>@permission</tt>, the initial state of the input
      # field will reflect the value of that variable's attribute <tt>@permission.admin</tt>.
      #
      # Alternatively, you can pass just the model object itself (if the first
      # argument isn't a string or symbol +fields_for+ will realize that the
      # name has been omitted) -
      #
      #   <%= fields_for @person.permission do |permission_fields| %>
      #     Admin?: <%= permission_fields.check_box :admin %>
      #   <% end %>
      #
      # and +fields_for+ will derive the required name of the field from the
      # _class_ of the model object, e.g. if <tt>@person.permission</tt>, is
      # of class +Permission+, the field will still be named <tt>permission[admin]</tt>.
      #
      # Note: This also works for the methods in FormOptionsHelper and
      # DateHelper that are designed to work with an object as base, like
      # FormOptionsHelper#collection_select and DateHelper#datetime_select.
      #
      # === Nested Attributes Examples
      #
      # When the object belonging to the current scope has a nested attribute
      # writer for a certain attribute, fields_for will yield a new scope
      # for that attribute. This allows you to create forms that set or change
      # the attributes of a parent object and its associations in one go.
      #
      # Nested attribute writers are normal setter methods named after an
      # association. The most common way of defining these writers is either
      # with +accepts_nested_attributes_for+ in a model definition or by
      # defining a method with the proper name. For example: the attribute
      # writer for the association <tt>:address</tt> is called
      # <tt>address_attributes=</tt>.
      #
      # Whether a one-to-one or one-to-many style form builder will be yielded
      # depends on whether the normal reader method returns a _single_ object
      # or an _array_ of objects.
      #
      # ==== One-to-one
      #
      # Consider a Person class which returns a _single_ Address from the
      # <tt>address</tt> reader method and responds to the
      # <tt>address_attributes=</tt> writer method:
      #
      #   class Person
      #     def address
      #       @address
      #     end
      #
      #     def address_attributes=(attributes)
      #       # Process the attributes hash
      #     end
      #   end
      #
      # This model can now be used with a nested fields_for, like so:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :address do |address_fields| %>
      #       Street  : <%= address_fields.text_field :street %>
      #       Zip code: <%= address_fields.text_field :zip_code %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # When address is already an association on a Person you can use
      # +accepts_nested_attributes_for+ to define the writer method for you:
      #
      #   class Person < ActiveRecord::Base
      #     has_one :address
      #     accepts_nested_attributes_for :address
      #   end
      #
      # If you want to destroy the associated model through the form, you have
      # to enable it first using the <tt>:allow_destroy</tt> option for
      # +accepts_nested_attributes_for+:
      #
      #   class Person < ActiveRecord::Base
      #     has_one :address
      #     accepts_nested_attributes_for :address, allow_destroy: true
      #   end
      #
      # Now, when you use a form element with the <tt>_destroy</tt> parameter,
      # with a value that evaluates to +true+, you will destroy the associated
      # model (e.g. 1, '1', true, or 'true'):
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :address do |address_fields| %>
      #       ...
      #       Delete: <%= address_fields.check_box :_destroy %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # ==== One-to-many
      #
      # Consider a Person class which returns an _array_ of Project instances
      # from the <tt>projects</tt> reader method and responds to the
      # <tt>projects_attributes=</tt> writer method:
      #
      #   class Person
      #     def projects
      #       [@project1, @project2]
      #     end
      #
      #     def projects_attributes=(attributes)
      #       # Process the attributes hash
      #     end
      #   end
      #
      # Note that the <tt>projects_attributes=</tt> writer method is in fact
      # required for fields_for to correctly identify <tt>:projects</tt> as a
      # collection, and the correct indices to be set in the form markup.
      #
      # When projects is already an association on Person you can use
      # +accepts_nested_attributes_for+ to define the writer method for you:
      #
      #   class Person < ActiveRecord::Base
      #     has_many :projects
      #     accepts_nested_attributes_for :projects
      #   end
      #
      # This model can now be used with a nested fields_for. The block given to
      # the nested fields_for call will be repeated for each instance in the
      # collection:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       <% if project_fields.object.active? %>
      #         Name: <%= project_fields.text_field :name %>
      #       <% end %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # It's also possible to specify the instance to be used:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <% @person.projects.each do |project| %>
      #       <% if project.active? %>
      #         <%= person_form.fields_for :projects, project do |project_fields| %>
      #           Name: <%= project_fields.text_field :name %>
      #         <% end %>
      #       <% end %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # Or a collection to be used:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects, @active_projects do |project_fields| %>
      #       Name: <%= project_fields.text_field :name %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # If you want to destroy any of the associated models through the
      # form, you have to enable it first using the <tt>:allow_destroy</tt>
      # option for +accepts_nested_attributes_for+:
      #
      #   class Person < ActiveRecord::Base
      #     has_many :projects
      #     accepts_nested_attributes_for :projects, allow_destroy: true
      #   end
      #
      # This will allow you to specify which models to destroy in the
      # attributes hash by adding a form element for the <tt>_destroy</tt>
      # parameter with a value that evaluates to +true+
      # (e.g. 1, '1', true, or 'true'):
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       Delete: <%= project_fields.check_box :_destroy %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # When a collection is used you might want to know the index of each
      # object in the array. For this purpose, the <tt>index</tt> method is
      # available in the FormBuilder object.
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       Project #<%= project_fields.index %>
      #       ...
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # Note that fields_for will automatically generate a hidden field
      # to store the ID of the record if it responds to <tt>persisted?</tt>.
      # There are circumstances where this hidden field is not needed and you
      # can pass <tt>include_id: false</tt> to prevent fields_for from
      # rendering it automatically.
      def fields_for(record_name, record_object = nil, options = {}, &block)
        options = { model: record_object, allow_method_names_outside_object: false, skip_default_ids: false }.merge!(options)

        fields(record_name, **options, &block)
      end

      # Scopes input fields with either an explicit scope or model.
      # Like +form_with+ does with <tt>:scope</tt> or <tt>:model</tt>,
      # except it doesn't output the form tags.
      #
      #   # Using a scope prefixes the input field names:
      #   <%= fields :comment do |fields| %>
      #     <%= fields.text_field :body %>
      #   <% end %>
      #   # => <input type="text" name="comment[body]">
      #
      #   # Using a model infers the scope and assigns field values:
      #   <%= fields model: Comment.new(body: "full bodied") do |fields| %>
      #     <%= fields.text_field :body %>
      #   <% end %>
      #   # => <input type="text" name="comment[body]" value="full bodied">
      #
      #   # Using +fields+ with +form_with+:
      #   <%= form_with model: @post do |form| %>
      #     <%= form.text_field :title %>
      #
      #     <%= form.fields :comment do |fields| %>
      #       <%= fields.text_field :body %>
      #     <% end %>
      #   <% end %>
      #
      # Much like +form_with+ a FormBuilder instance associated with the scope
      # or model is yielded, so any generated field names are prefixed with
      # either the passed scope or the scope inferred from the <tt>:model</tt>.
      #
      # === Mixing with other form helpers
      #
      # While +form_with+ uses a FormBuilder object it's possible to mix and
      # match the stand-alone FormHelper methods and methods
      # from FormTagHelper:
      #
      #   <%= fields model: @comment do |fields| %>
      #     <%= fields.text_field :body %>
      #
      #     <%= text_area :commenter, :biography %>
      #     <%= check_box_tag "comment[all_caps]", "1", @comment.commenter.hulk_mode? %>
      #   <% end %>
      #
      # Same goes for the methods in FormOptionsHelper and DateHelper designed
      # to work with an object as a base, like
      # FormOptionsHelper#collection_select and DateHelper#datetime_select.
      def fields(scope = nil, model: nil, **options, &block)
        options = { allow_method_names_outside_object: true, skip_default_ids: !form_with_generates_ids }.merge!(options)

        if model
          model   = _object_for_form_builder(model)
          scope ||= model_name_from_record_or_class(model).param_key
        end

        builder = instantiate_builder(scope, model, options)
        capture(builder, &block)
      end

      # Returns a label tag tailored for labelling an input field for a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). The text of label will default to the attribute name unless a translation
      # is found in the current I18n locale (through <tt>helpers.label.<modelname>.<attribute></tt>) or you specify it explicitly.
      # Additional options on the label tag can be passed as a hash with +options+. These options will be tagged
      # onto the HTML as an HTML element attribute as in the example shown, except for the <tt>:value</tt> option, which is designed to
      # target labels for radio_button tags (where the value is used in the ID of the input tag).
      #
      # ==== Examples
      #   label(:post, :title)
      #   # => <label for="post_title">Title</label>
      #
      # You can localize your labels based on model and attribute names.
      # For example you can define the following in your locale (e.g. en.yml)
      #
      #   helpers:
      #     label:
      #       post:
      #         body: "Write your entire text here"
      #
      # Which then will result in
      #
      #   label(:post, :body)
      #   # => <label for="post_body">Write your entire text here</label>
      #
      # Localization can also be based purely on the translation of the attribute-name
      # (if you are using ActiveRecord):
      #
      #   activerecord:
      #     attributes:
      #       post:
      #         cost: "Total cost"
      #
      # <code></code>
      #
      #   label(:post, :cost)
      #   # => <label for="post_cost">Total cost</label>
      #
      #   label(:post, :title, "A short title")
      #   # => <label for="post_title">A short title</label>
      #
      #   label(:post, :title, "A short title", class: "title_label")
      #   # => <label for="post_title" class="title_label">A short title</label>
      #
      #   label(:post, :privacy, "Public Post", value: "public")
      #   # => <label for="post_privacy_public">Public Post</label>
      #
      #   label(:post, :cost) do |translation|
      #     content_tag(:span, translation, class: "cost_label")
      #   end
      #   # => <label for="post_cost"><span class="cost_label">Total cost</span></label>
      #
      #   label(:post, :cost) do |builder|
      #     content_tag(:span, builder.translation, class: "cost_label")
      #   end
      #   # => <label for="post_cost"><span class="cost_label">Total cost</span></label>
      #
      #   label(:post, :terms) do
      #     raw('Accept <a href="/terms">Terms</a>.')
      #   end
      #   # => <label for="post_terms">Accept <a href="/terms">Terms</a>.</label>
      def label(object_name, method, content_or_options = nil, options = nil, &block)
        Tags::Label.new(object_name, method, self, content_or_options, options).render(&block)
      end

      # Returns an input tag of the "text" type tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown.
      #
      # ==== Examples
      #   text_field(:post, :title, size: 20)
      #   # => <input type="text" id="post_title" name="post[title]" size="20" value="#{@post.title}" />
      #
      #   text_field(:post, :title, class: "create_input")
      #   # => <input type="text" id="post_title" name="post[title]" value="#{@post.title}" class="create_input" />
      #
      #   text_field(:post, :title,  maxlength: 30, class: "title_input")
      #   # => <input type="text" id="post_title" name="post[title]" maxlength="30" size="30" value="#{@post.title}" class="title_input" />
      #
      #   text_field(:session, :user, onchange: "if ($('#session_user').val() === 'admin') { alert('Your login cannot be admin!'); }")
      #   # => <input type="text" id="session_user" name="session[user]" value="#{@session.user}" onchange="if ($('#session_user').val() === 'admin') { alert('Your login cannot be admin!'); }"/>
      #
      #   text_field(:snippet, :code, size: 20, class: 'code_input')
      #   # => <input type="text" id="snippet_code" name="snippet[code]" size="20" value="#{@snippet.code}" class="code_input" />
      def text_field(object_name, method, options = {})
        Tags::TextField.new(object_name, method, self, options).render
      end

      # Returns an input tag of the "password" type tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown. For security reasons this field is blank by default; pass in a value via +options+ if this is not desired.
      #
      # ==== Examples
      #   password_field(:login, :pass, size: 20)
      #   # => <input type="password" id="login_pass" name="login[pass]" size="20" />
      #
      #   password_field(:account, :secret, class: "form_input", value: @account.secret)
      #   # => <input type="password" id="account_secret" name="account[secret]" value="#{@account.secret}" class="form_input" />
      #
      #   password_field(:user, :password, onchange: "if ($('#user_password').val().length > 30) { alert('Your password needs to be shorter!'); }")
      #   # => <input type="password" id="user_password" name="user[password]" onchange="if ($('#user_password').val().length > 30) { alert('Your password needs to be shorter!'); }"/>
      #
      #   password_field(:account, :pin, size: 20, class: 'form_input')
      #   # => <input type="password" id="account_pin" name="account[pin]" size="20" class="form_input" />
      def password_field(object_name, method, options = {})
        Tags::PasswordField.new(object_name, method, self, options).render
      end

      # Returns a hidden input tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown.
      #
      # ==== Examples
      #   hidden_field(:signup, :pass_confirm)
      #   # => <input type="hidden" id="signup_pass_confirm" name="signup[pass_confirm]" value="#{@signup.pass_confirm}" />
      #
      #   hidden_field(:post, :tag_list)
      #   # => <input type="hidden" id="post_tag_list" name="post[tag_list]" value="#{@post.tag_list}" />
      #
      #   hidden_field(:user, :token)
      #   # => <input type="hidden" id="user_token" name="user[token]" value="#{@user.token}" />
      def hidden_field(object_name, method, options = {})
        Tags::HiddenField.new(object_name, method, self, options).render
      end

      # Returns a file upload input tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown.
      #
      # Using this method inside a +form_for+ block will set the enclosing form's encoding to <tt>multipart/form-data</tt>.
      #
      # ==== Options
      # * Creates standard HTML attributes for the tag.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:multiple</tt> - If set to true, *in most updated browsers* the user will be allowed to select multiple files.
      # * <tt>:include_hidden</tt> - When <tt>multiple: true</tt> and <tt>include_hidden: true</tt>, the field will be prefixed with an <tt><input type="hidden"></tt> field with an empty value to support submitting an empty collection of files.
      # * <tt>:accept</tt> - If set to one or multiple mime-types, the user will be suggested a filter when choosing a file. You still need to set up model validations.
      #
      # ==== Examples
      #   file_field(:user, :avatar)
      #   # => <input type="file" id="user_avatar" name="user[avatar]" />
      #
      #   file_field(:post, :image, multiple: true)
      #   # => <input type="file" id="post_image" name="post[image][]" multiple="multiple" />
      #
      #   file_field(:post, :attached, accept: 'text/html')
      #   # => <input accept="text/html" type="file" id="post_attached" name="post[attached]" />
      #
      #   file_field(:post, :image, accept: 'image/png,image/gif,image/jpeg')
      #   # => <input type="file" id="post_image" name="post[image]" accept="image/png,image/gif,image/jpeg" />
      #
      #   file_field(:attachment, :file, class: 'file_input')
      #   # => <input type="file" id="attachment_file" name="attachment[file]" class="file_input" />
      def file_field(object_name, method, options = {})
        options = { include_hidden: multiple_file_field_include_hidden }.merge!(options)

        Tags::FileField.new(object_name, method, self, convert_direct_upload_option_to_url(options.dup)).render
      end

      # Returns a textarea opening and closing tag set tailored for accessing a specified attribute (identified by +method+)
      # on an object assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+.
      #
      # ==== Examples
      #   text_area(:post, :body, cols: 20, rows: 40)
      #   # => <textarea cols="20" rows="40" id="post_body" name="post[body]">
      #   #      #{@post.body}
      #   #    </textarea>
      #
      #   text_area(:comment, :text, size: "20x30")
      #   # => <textarea cols="20" rows="30" id="comment_text" name="comment[text]">
      #   #      #{@comment.text}
      #   #    </textarea>
      #
      #   text_area(:application, :notes, cols: 40, rows: 15, class: 'app_input')
      #   # => <textarea cols="40" rows="15" id="application_notes" name="application[notes]" class="app_input">
      #   #      #{@application.notes}
      #   #    </textarea>
      #
      #   text_area(:entry, :body, size: "20x20", disabled: 'disabled')
      #   # => <textarea cols="20" rows="20" id="entry_body" name="entry[body]" disabled="disabled">
      #   #      #{@entry.body}
      #   #    </textarea>
      def text_area(object_name, method, options = {})
        Tags::TextArea.new(object_name, method, self, options).render
      end

      # Returns a checkbox tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). This object must be an instance object (@object) and not a local object.
      # It's intended that +method+ returns an integer and if that integer is above zero, then the checkbox is checked.
      # Additional options on the input tag can be passed as a hash with +options+. The +checked_value+ defaults to 1
      # while the default +unchecked_value+ is set to 0 which is convenient for boolean values.
      #
      # ==== Options
      #
      # * Any standard HTML attributes for the tag can be passed in, for example +:class+.
      # * <tt>:checked</tt> - +true+ or +false+ forces the state of the checkbox to be checked or not.
      # * <tt>:include_hidden</tt> - If set to false, the auxiliary hidden field described below will not be generated.
      #
      # ==== Gotcha
      #
      # The HTML specification says unchecked check boxes are not successful, and
      # thus web browsers do not send them. Unfortunately this introduces a gotcha:
      # if an +Invoice+ model has a +paid+ flag, and in the form that edits a paid
      # invoice the user unchecks its check box, no +paid+ parameter is sent. So,
      # any mass-assignment idiom like
      #
      #   @invoice.update(params[:invoice])
      #
      # wouldn't update the flag.
      #
      # To prevent this the helper generates an auxiliary hidden field before
      # every check box. The hidden field has the same name and its
      # attributes mimic an unchecked check box.
      #
      # This way, the client either sends only the hidden field (representing
      # the check box is unchecked), or both fields. Since the HTML specification
      # says key/value pairs have to be sent in the same order they appear in the
      # form, and parameters extraction gets the last occurrence of any repeated
      # key in the query string, that works for ordinary forms.
      #
      # Unfortunately that workaround does not work when the check box goes
      # within an array-like parameter, as in
      #
      #   <%= fields_for "project[invoice_attributes][]", invoice, index: nil do |form| %>
      #     <%= form.check_box :paid %>
      #     ...
      #   <% end %>
      #
      # because parameter name repetition is precisely what \Rails seeks to distinguish
      # the elements of the array. For each item with a checked check box you
      # get an extra ghost item with only that attribute, assigned to "0".
      #
      # In that case it is preferable to either use +check_box_tag+ or to use
      # hashes instead of arrays.
      #
      # ==== Examples
      #
      #   # Let's say that @post.validated? is 1:
      #   check_box("post", "validated")
      #   # => <input name="post[validated]" type="hidden" value="0" />
      #   #    <input checked="checked" type="checkbox" id="post_validated" name="post[validated]" value="1" />
      #
      #   # Let's say that @puppy.gooddog is "no":
      #   check_box("puppy", "gooddog", {}, "yes", "no")
      #   # => <input name="puppy[gooddog]" type="hidden" value="no" />
      #   #    <input type="checkbox" id="puppy_gooddog" name="puppy[gooddog]" value="yes" />
      #
      #   check_box("eula", "accepted", { class: 'eula_check' }, "yes", "no")
      #   # => <input name="eula[accepted]" type="hidden" value="no" />
      #   #    <input type="checkbox" class="eula_check" id="eula_accepted" name="eula[accepted]" value="yes" />
      def check_box(object_name, method, options = {}, checked_value = "1", unchecked_value = "0")
        Tags::CheckBox.new(object_name, method, self, checked_value, unchecked_value, options).render
      end

      # Returns a radio button tag for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). If the current value of +method+ is +tag_value+ the
      # radio button will be checked.
      #
      # To force the radio button to be checked pass <tt>checked: true</tt> in the
      # +options+ hash. You may pass HTML options there as well.
      #
      #   # Let's say that @post.category returns "rails":
      #   radio_button("post", "category", "rails")
      #   radio_button("post", "category", "java")
      #   # => <input type="radio" id="post_category_rails" name="post[category]" value="rails" checked="checked" />
      #   #    <input type="radio" id="post_category_java" name="post[category]" value="java" />
      #
      #   # Let's say that @user.receive_newsletter returns "no":
      #   radio_button("user", "receive_newsletter", "yes")
      #   radio_button("user", "receive_newsletter", "no")
      #   # => <input type="radio" id="user_receive_newsletter_yes" name="user[receive_newsletter]" value="yes" />
      #   #    <input type="radio" id="user_receive_newsletter_no" name="user[receive_newsletter]" value="no" checked="checked" />
      def radio_button(object_name, method, tag_value, options = {})
        Tags::RadioButton.new(object_name, method, self, tag_value, options).render
      end

      # Returns a text_field of type "color".
      #
      #   color_field("car", "color")
      #   # => <input id="car_color" name="car[color]" type="color" value="#000000" />
      def color_field(object_name, method, options = {})
        Tags::ColorField.new(object_name, method, self, options).render
      end

      # Returns an input of type "search" for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object_name+). Inputs of type "search" may be styled differently by
      # some browsers.
      #
      #   search_field(:user, :name)
      #   # => <input id="user_name" name="user[name]" type="search" />
      #   search_field(:user, :name, autosave: false)
      #   # => <input autosave="false" id="user_name" name="user[name]" type="search" />
      #   search_field(:user, :name, results: 3)
      #   # => <input id="user_name" name="user[name]" results="3" type="search" />
      #   #  Assume request.host returns "www.example.com"
      #   search_field(:user, :name, autosave: true)
      #   # => <input autosave="com.example.www" id="user_name" name="user[name]" results="10" type="search" />
      #   search_field(:user, :name, onsearch: true)
      #   # => <input id="user_name" incremental="true" name="user[name]" onsearch="true" type="search" />
      #   search_field(:user, :name, autosave: false, onsearch: true)
      #   # => <input autosave="false" id="user_name" incremental="true" name="user[name]" onsearch="true" type="search" />
      #   search_field(:user, :name, autosave: true, onsearch: true)
      #   # => <input autosave="com.example.www" id="user_name" incremental="true" name="user[name]" onsearch="true" results="10" type="search" />
      def search_field(object_name, method, options = {})
        Tags::SearchField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "tel".
      #
      #   telephone_field("user", "phone")
      #   # => <input id="user_phone" name="user[phone]" type="tel" />
      #
      def telephone_field(object_name, method, options = {})
        Tags::TelField.new(object_name, method, self, options).render
      end
      # aliases telephone_field
      alias phone_field telephone_field

      # Returns a text_field of type "date".
      #
      #   date_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="date" />
      #
      # The default value is generated by trying to call +strftime+ with "%Y-%m-%d"
      # on the object's value, which makes it behave as expected for instances
      # of DateTime and ActiveSupport::TimeWithZone. You can still override that
      # by passing the "value" option explicitly, e.g.
      #
      #   @user.born_on = Date.new(1984, 1, 27)
      #   date_field("user", "born_on", value: "1984-05-12")
      #   # => <input id="user_born_on" name="user[born_on]" type="date" value="1984-05-12" />
      #
      # You can create values for the "min" and "max" attributes by passing
      # instances of Date or Time to the options hash.
      #
      #   date_field("user", "born_on", min: Date.today)
      #   # => <input id="user_born_on" name="user[born_on]" type="date" min="2014-05-20" />
      #
      # Alternatively, you can pass a String formatted as an ISO8601 date as the
      # values for "min" and "max."
      #
      #   date_field("user", "born_on", min: "2014-05-20")
      #   # => <input id="user_born_on" name="user[born_on]" type="date" min="2014-05-20" />
      #
      def date_field(object_name, method, options = {})
        Tags::DateField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "time".
      #
      # The default value is generated by trying to call +strftime+ with "%T.%L"
      # on the object's value. If you pass <tt>include_seconds: false</tt>, it will be
      # formatted by trying to call +strftime+ with "%H:%M" on the object's value.
      # It is also possible to override this by passing the "value" option.
      #
      # ==== Options
      #
      # Supports the same options as FormTagHelper#time_field_tag.
      #
      # ==== Examples
      #
      #   time_field("task", "started_at")
      #   # => <input id="task_started_at" name="task[started_at]" type="time" />
      #
      # You can create values for the "min" and "max" attributes by passing
      # instances of Date or Time to the options hash.
      #
      #   time_field("task", "started_at", min: Time.now)
      #   # => <input id="task_started_at" name="task[started_at]" type="time" min="01:00:00.000" />
      #
      # Alternatively, you can pass a String formatted as an ISO8601 time as the
      # values for "min" and "max."
      #
      #   time_field("task", "started_at", min: "01:00:00")
      #   # => <input id="task_started_at" name="task[started_at]" type="time" min="01:00:00.000" />
      #
      # By default, provided times will be formatted including seconds. You can render just the hour
      # and minute by passing <tt>include_seconds: false</tt>. Some browsers will render a simpler UI
      # if you exclude seconds in the timestamp format.
      #
      #   time_field("task", "started_at", value: Time.now, include_seconds: false)
      #   # => <input id="task_started_at" name="task[started_at]" type="time" value="01:00" />
      def time_field(object_name, method, options = {})
        Tags::TimeField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "datetime-local".
      #
      #   datetime_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="datetime-local" />
      #
      # The default value is generated by trying to call +strftime+ with "%Y-%m-%dT%T"
      # on the object's value, which makes it behave as expected for instances
      # of DateTime and ActiveSupport::TimeWithZone.
      #
      #   @user.born_on = Date.new(1984, 1, 12)
      #   datetime_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="datetime-local" value="1984-01-12T00:00:00" />
      #
      # You can create values for the "min" and "max" attributes by passing
      # instances of Date or Time to the options hash.
      #
      #   datetime_field("user", "born_on", min: Date.today)
      #   # => <input id="user_born_on" name="user[born_on]" type="datetime-local" min="2014-05-20T00:00:00.000" />
      #
      # Alternatively, you can pass a String formatted as an ISO8601 datetime as
      # the values for "min" and "max."
      #
      #   datetime_field("user", "born_on", min: "2014-05-20T00:00:00")
      #   # => <input id="user_born_on" name="user[born_on]" type="datetime-local" min="2014-05-20T00:00:00.000" />
      #
      # By default, provided datetimes will be formatted including seconds. You can render just the date, hour,
      # and minute by passing <tt>include_seconds: false</tt>.
      #
      #   @user.born_on = Time.current
      #   datetime_field("user", "born_on", include_seconds: false)
      #   # => <input id="user_born_on" name="user[born_on]" type="datetime-local" value="2014-05-20T14:35" />
      def datetime_field(object_name, method, options = {})
        Tags::DatetimeLocalField.new(object_name, method, self, options).render
      end

      alias datetime_local_field datetime_field

      # Returns a text_field of type "month".
      #
      #   month_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="month" />
      #
      # The default value is generated by trying to call +strftime+ with "%Y-%m"
      # on the object's value, which makes it behave as expected for instances
      # of DateTime and ActiveSupport::TimeWithZone.
      #
      #   @user.born_on = Date.new(1984, 1, 27)
      #   month_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="date" value="1984-01" />
      #
      def month_field(object_name, method, options = {})
        Tags::MonthField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "week".
      #
      #   week_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="week" />
      #
      # The default value is generated by trying to call +strftime+ with "%Y-W%W"
      # on the object's value, which makes it behave as expected for instances
      # of DateTime and ActiveSupport::TimeWithZone.
      #
      #   @user.born_on = Date.new(1984, 5, 12)
      #   week_field("user", "born_on")
      #   # => <input id="user_born_on" name="user[born_on]" type="date" value="1984-W19" />
      #
      def week_field(object_name, method, options = {})
        Tags::WeekField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "url".
      #
      #   url_field("user", "homepage")
      #   # => <input id="user_homepage" name="user[homepage]" type="url" />
      #
      def url_field(object_name, method, options = {})
        Tags::UrlField.new(object_name, method, self, options).render
      end

      # Returns a text_field of type "email".
      #
      #   email_field("user", "address")
      #   # => <input id="user_address" name="user[address]" type="email" />
      #
      def email_field(object_name, method, options = {})
        Tags::EmailField.new(object_name, method, self, options).render
      end

      # Returns an input tag of type "number".
      #
      # ==== Options
      #
      # Supports the same options as FormTagHelper#number_field_tag.
      def number_field(object_name, method, options = {})
        Tags::NumberField.new(object_name, method, self, options).render
      end

      # Returns an input tag of type "range".
      #
      # ==== Options
      #
      # Supports the same options as FormTagHelper#range_field_tag.
      def range_field(object_name, method, options = {})
        Tags::RangeField.new(object_name, method, self, options).render
      end

      def _object_for_form_builder(object) # :nodoc:
        object.is_a?(Array) ? object.last : object
      end

      private
        def html_options_for_form_with(url_for_options = nil, model = nil, html: {}, local: !form_with_generates_remote_forms,
          skip_enforcing_utf8: nil, **options)
          html_options = options.slice(:id, :class, :multipart, :method, :data, :authenticity_token).merge!(html)
          html_options[:remote] = html.delete(:remote) || !local
          html_options[:method] ||= :patch if model.respond_to?(:persisted?) && model.persisted?
          if skip_enforcing_utf8.nil?
            if options.key?(:enforce_utf8)
              html_options[:enforce_utf8] = options[:enforce_utf8]
            end
          else
            html_options[:enforce_utf8] = !skip_enforcing_utf8
          end
          html_options_for_form(url_for_options.nil? ? {} : url_for_options, html_options)
        end

        def instantiate_builder(record_name, record_object, options)
          case record_name
          when String, Symbol
            object = record_object
            object_name = record_name
          else
            object = record_name
            object_name = model_name_from_record_or_class(object).param_key if object
          end

          builder = options[:builder] || default_form_builder_class
          builder.new(object_name, object, self, options)
        end

        def default_form_builder_class
          builder = default_form_builder || ActionView::Base.default_form_builder
          builder.respond_to?(:constantize) ? builder.constantize : builder
        end
    end

    # = Action View Form Builder
    #
    # A +FormBuilder+ object is associated with a particular model object and
    # allows you to generate fields associated with the model object. The
    # +FormBuilder+ object is yielded when using +form_for+ or +fields_for+.
    # For example:
    #
    #   <%= form_for @person do |person_form| %>
    #     Name: <%= person_form.text_field :name %>
    #     Admin: <%= person_form.check_box :admin %>
    #   <% end %>
    #
    # In the above block, a +FormBuilder+ object is yielded as the
    # +person_form+ variable. This allows you to generate the +text_field+
    # and +check_box+ fields by specifying their eponymous methods, which
    # modify the underlying template and associates the <tt>@person</tt> model object
    # with the form.
    #
    # The +FormBuilder+ object can be thought of as serving as a proxy for the
    # methods in the +FormHelper+ module. This class, however, allows you to
    # call methods with the model object you are building the form for.
    #
    # You can create your own custom FormBuilder templates by subclassing this
    # class. For example:
    #
    #   class MyFormBuilder < ActionView::Helpers::FormBuilder
    #     def div_radio_button(method, tag_value, options = {})
    #       @template.content_tag(:div,
    #         @template.radio_button(
    #           @object_name, method, tag_value, objectify_options(options)
    #         )
    #       )
    #     end
    #   end
    #
    # The above code creates a new method +div_radio_button+ which wraps a div
    # around the new radio button. Note that when options are passed in, you
    # must call +objectify_options+ in order for the model object to get
    # correctly passed to the method. If +objectify_options+ is not called,
    # then the newly created helper will not be linked back to the model.
    #
    # The +div_radio_button+ code from above can now be used as follows:
    #
    #   <%= form_for @person, :builder => MyFormBuilder do |f| %>
    #     I am a child: <%= f.div_radio_button(:admin, "child") %>
    #     I am an adult: <%= f.div_radio_button(:admin, "adult") %>
    #   <% end -%>
    #
    # The standard set of helper methods for form building are located in the
    # +field_helpers+ class attribute.
    class FormBuilder
      include ModelNaming

      # The methods which wrap a form helper call.
      class_attribute :field_helpers, default: [
        :fields_for, :fields, :label, :text_field, :password_field,
        :hidden_field, :file_field, :text_area, :check_box,
        :radio_button, :color_field, :search_field,
        :telephone_field, :phone_field, :date_field,
        :time_field, :datetime_field, :datetime_local_field,
        :month_field, :week_field, :url_field, :email_field,
        :number_field, :range_field
      ]

      attr_accessor :object_name, :object, :options

      attr_reader :multipart, :index
      alias :multipart? :multipart

      def multipart=(multipart)
        @multipart = multipart

        if parent_builder = @options[:parent_builder]
          parent_builder.multipart = multipart
        end
      end

      def self._to_partial_path
        @_to_partial_path ||= name.demodulize.underscore.sub!(/_builder$/, "")
      end

      def to_partial_path
        self.class._to_partial_path
      end

      def to_model
        self
      end

      def initialize(object_name, object, template, options)
        @nested_child_index = {}
        @object_name, @object, @template, @options = object_name, object, template, options
        @default_options = @options ? @options.slice(:index, :namespace, :skip_default_ids, :allow_method_names_outside_object) : {}
        @default_html_options = @default_options.except(:skip_default_ids, :allow_method_names_outside_object)

        convert_to_legacy_options(@options)

        if @object_name&.end_with?("[]")
          if (object ||= @template.instance_variable_get("@#{@object_name[0..-3]}")) && object.respond_to?(:to_param)
            @auto_index = object.to_param
          else
            raise ArgumentError, "object[] naming but object param and @object var don't exist or don't respond to to_param: #{object.inspect}"
          end
        end

        @multipart = nil
        @index = options[:index] || options[:child_index]
      end

      # Generate an HTML <tt>id</tt> attribute value.
      #
      # return the <tt><form></tt> element's <tt>id</tt> attribute.
      #
      #   <%= form_for @post do |f| %>
      #     <%# ... %>
      #
      #     <% content_for :sticky_footer do %>
      #       <%= form.button(form: f.id) %>
      #     <% end %>
      #   <% end %>
      #
      # In the example above, the <tt>:sticky_footer</tt> content area will
      # exist outside of the <tt><form></tt> element. By declaring the
      # <tt>form</tt> HTML attribute, we hint to the browser that the generated
      # <tt><button></tt> element should be treated as the <tt><form></tt>
      # element's submit button, regardless of where it exists in the DOM.
      def id
        options.dig(:html, :id) || options[:id]
      end

      # Generate an HTML <tt>id</tt> attribute value for the given field
      #
      # Return the value generated by the <tt>FormBuilder</tt> for the given
      # attribute name.
      #
      #   <%= form_for @post do |f| %>
      #     <%= f.label :title %>
      #     <%= f.text_field :title, aria: { describedby: f.field_id(:title, :error) } %>
      #     <%= tag.span("is blank", id: f.field_id(:title, :error) %>
      #   <% end %>
      #
      # In the example above, the <tt><input type="text"></tt> element built by
      # the call to <tt>FormBuilder#text_field</tt> declares an
      # <tt>aria-describedby</tt> attribute referencing the <tt><span></tt>
      # element, sharing a common <tt>id</tt> root (<tt>post_title</tt>, in this
      # case).
      def field_id(method, *suffixes, namespace: @options[:namespace], index: @options[:index])
        @template.field_id(@object_name, method, *suffixes, namespace: namespace, index: index)
      end

      # Generate an HTML <tt>name</tt> attribute value for the given name and
      # field combination
      #
      # Return the value generated by the <tt>FormBuilder</tt> for the given
      # attribute name.
      #
      #   <%= form_for @post do |f| %>
      #     <%= f.text_field :title, name: f.field_name(:title, :subtitle) %>
      #     <%# => <input type="text" name="post[title][subtitle]"> %>
      #   <% end %>
      #
      #   <%= form_for @post do |f| %>
      #     <%= f.text_field :tag, name: f.field_name(:tag, multiple: true) %>
      #     <%# => <input type="text" name="post[tag][]"> %>
      #   <% end %>
      #
      def field_name(method, *methods, multiple: false, index: @options[:index])
        object_name = @options.fetch(:as) { @object_name }

        @template.field_name(object_name, method, *methods, index: index, multiple: multiple)
      end

      ##
      # :method: text_field
      #
      # :call-seq: text_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#text_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.text_field :name %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: password_field
      #
      # :call-seq: password_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#password_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.password_field :password %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: text_area
      #
      # :call-seq: text_area(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#text_area for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.text_area :detail %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: color_field
      #
      # :call-seq: color_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#color_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.color_field :favorite_color %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: search_field
      #
      # :call-seq: search_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#search_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.search_field :name %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: telephone_field
      #
      # :call-seq: telephone_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#telephone_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.telephone_field :phone %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: phone_field
      #
      # :call-seq: phone_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#phone_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.phone_field :phone %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: date_field
      #
      # :call-seq: date_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#date_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.date_field :born_on %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: time_field
      #
      # :call-seq: time_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#time_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.time_field :born_at %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: datetime_field
      #
      # :call-seq: datetime_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#datetime_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.datetime_field :graduation_day %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: datetime_local_field
      #
      # :call-seq: datetime_local_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#datetime_local_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.datetime_local_field :graduation_day %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: month_field
      #
      # :call-seq: month_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#month_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.month_field :birthday_month %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: week_field
      #
      # :call-seq: week_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#week_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.week_field :birthday_week %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: url_field
      #
      # :call-seq: url_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#url_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.url_field :homepage %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: email_field
      #
      # :call-seq: email_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#email_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.email_field :address %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: number_field
      #
      # :call-seq: number_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#number_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.number_field :age %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      ##
      # :method: range_field
      #
      # :call-seq: range_field(method, options = {})
      #
      # Wraps ActionView::Helpers::FormHelper#range_field for form builders:
      #
      #   <%= form_with model: @user do |f| %>
      #     <%= f.range_field :age %>
      #   <% end %>
      #
      # Please refer to the documentation of the base helper for details.

      (field_helpers - [:label, :check_box, :radio_button, :fields_for, :fields, :hidden_field, :file_field]).each do |selector|
        class_eval <<-RUBY_EVAL, __FILE__, __LINE__ + 1
          def #{selector}(method, options = {})  # def text_field(method, options = {})
            @template.public_send(               #   @template.public_send(
              #{selector.inspect},               #     :text_field,
              @object_name,                      #     @object_name,
              method,                            #     method,
              objectify_options(options))        #     objectify_options(options))
          end                                    # end
        RUBY_EVAL
      end

      # Creates a scope around a specific model object like form_for, but
      # doesn't create the form tags themselves. This makes fields_for suitable
      # for specifying additional model objects in the same form.
      #
      # Although the usage and purpose of +fields_for+ is similar to +form_for+'s,
      # its method signature is slightly different. Like +form_for+, it yields
      # a FormBuilder object associated with a particular model object to a block,
      # and within the block allows methods to be called on the builder to
      # generate fields associated with the model object. Fields may reflect
      # a model object in two ways - how they are named (hence how submitted
      # values appear within the +params+ hash in the controller) and what
      # default values are shown when the form the fields appear in is first
      # displayed. In order for both of these features to be specified independently,
      # both an object name (represented by either a symbol or string) and the
      # object itself can be passed to the method separately -
      #
      #   <%= form_for @person do |person_form| %>
      #     First name: <%= person_form.text_field :first_name %>
      #     Last name : <%= person_form.text_field :last_name %>
      #
      #     <%= fields_for :permission, @person.permission do |permission_fields| %>
      #       Admin?  : <%= permission_fields.check_box :admin %>
      #     <% end %>
      #
      #     <%= person_form.submit %>
      #   <% end %>
      #
      # In this case, the checkbox field will be represented by an HTML +input+
      # tag with the +name+ attribute <tt>permission[admin]</tt>, and the submitted
      # value will appear in the controller as <tt>params[:permission][:admin]</tt>.
      # If <tt>@person.permission</tt> is an existing record with an attribute
      # +admin+, the initial state of the checkbox when first displayed will
      # reflect the value of <tt>@person.permission.admin</tt>.
      #
      # Often this can be simplified by passing just the name of the model
      # object to +fields_for+ -
      #
      #   <%= fields_for :permission do |permission_fields| %>
      #     Admin?: <%= permission_fields.check_box :admin %>
      #   <% end %>
      #
      # ...in which case, if <tt>:permission</tt> also happens to be the name of an
      # instance variable <tt>@permission</tt>, the initial state of the input
      # field will reflect the value of that variable's attribute <tt>@permission.admin</tt>.
      #
      # Alternatively, you can pass just the model object itself (if the first
      # argument isn't a string or symbol +fields_for+ will realize that the
      # name has been omitted) -
      #
      #   <%= fields_for @person.permission do |permission_fields| %>
      #     Admin?: <%= permission_fields.check_box :admin %>
      #   <% end %>
      #
      # and +fields_for+ will derive the required name of the field from the
      # _class_ of the model object, e.g. if <tt>@person.permission</tt>, is
      # of class +Permission+, the field will still be named <tt>permission[admin]</tt>.
      #
      # Note: This also works for the methods in FormOptionsHelper and
      # DateHelper that are designed to work with an object as base, like
      # FormOptionsHelper#collection_select and DateHelper#datetime_select.
      #
      # +fields_for+ tries to be smart about parameters, but it can be confused if both
      # name and value parameters are provided and the provided value has the shape of an
      # option Hash. To remove the ambiguity, explicitly pass an option Hash, even if empty.
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= fields_for :permission, @person.permission, {} do |permission_fields| %>
      #       Admin?: <%= check_box_tag permission_fields.field_name(:admin), @person.permission[:admin] %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # === Nested Attributes Examples
      #
      # When the object belonging to the current scope has a nested attribute
      # writer for a certain attribute, fields_for will yield a new scope
      # for that attribute. This allows you to create forms that set or change
      # the attributes of a parent object and its associations in one go.
      #
      # Nested attribute writers are normal setter methods named after an
      # association. The most common way of defining these writers is either
      # with +accepts_nested_attributes_for+ in a model definition or by
      # defining a method with the proper name. For example: the attribute
      # writer for the association <tt>:address</tt> is called
      # <tt>address_attributes=</tt>.
      #
      # Whether a one-to-one or one-to-many style form builder will be yielded
      # depends on whether the normal reader method returns a _single_ object
      # or an _array_ of objects.
      #
      # ==== One-to-one
      #
      # Consider a Person class which returns a _single_ Address from the
      # <tt>address</tt> reader method and responds to the
      # <tt>address_attributes=</tt> writer method:
      #
      #   class Person
      #     def address
      #       @address
      #     end
      #
      #     def address_attributes=(attributes)
      #       # Process the attributes hash
      #     end
      #   end
      #
      # This model can now be used with a nested fields_for, like so:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :address do |address_fields| %>
      #       Street  : <%= address_fields.text_field :street %>
      #       Zip code: <%= address_fields.text_field :zip_code %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # When address is already an association on a Person you can use
      # +accepts_nested_attributes_for+ to define the writer method for you:
      #
      #   class Person < ActiveRecord::Base
      #     has_one :address
      #     accepts_nested_attributes_for :address
      #   end
      #
      # If you want to destroy the associated model through the form, you have
      # to enable it first using the <tt>:allow_destroy</tt> option for
      # +accepts_nested_attributes_for+:
      #
      #   class Person < ActiveRecord::Base
      #     has_one :address
      #     accepts_nested_attributes_for :address, allow_destroy: true
      #   end
      #
      # Now, when you use a form element with the <tt>_destroy</tt> parameter,
      # with a value that evaluates to +true+, you will destroy the associated
      # model (e.g. 1, '1', true, or 'true'):
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :address do |address_fields| %>
      #       ...
      #       Delete: <%= address_fields.check_box :_destroy %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # ==== One-to-many
      #
      # Consider a Person class which returns an _array_ of Project instances
      # from the <tt>projects</tt> reader method and responds to the
      # <tt>projects_attributes=</tt> writer method:
      #
      #   class Person
      #     def projects
      #       [@project1, @project2]
      #     end
      #
      #     def projects_attributes=(attributes)
      #       # Process the attributes hash
      #     end
      #   end
      #
      # Note that the <tt>projects_attributes=</tt> writer method is in fact
      # required for fields_for to correctly identify <tt>:projects</tt> as a
      # collection, and the correct indices to be set in the form markup.
      #
      # When projects is already an association on Person you can use
      # +accepts_nested_attributes_for+ to define the writer method for you:
      #
      #   class Person < ActiveRecord::Base
      #     has_many :projects
      #     accepts_nested_attributes_for :projects
      #   end
      #
      # This model can now be used with a nested fields_for. The block given to
      # the nested fields_for call will be repeated for each instance in the
      # collection:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       <% if project_fields.object.active? %>
      #         Name: <%= project_fields.text_field :name %>
      #       <% end %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # It's also possible to specify the instance to be used:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <% @person.projects.each do |project| %>
      #       <% if project.active? %>
      #         <%= person_form.fields_for :projects, project do |project_fields| %>
      #           Name: <%= project_fields.text_field :name %>
      #         <% end %>
      #       <% end %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # Or a collection to be used:
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects, @active_projects do |project_fields| %>
      #       Name: <%= project_fields.text_field :name %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # If you want to destroy any of the associated models through the
      # form, you have to enable it first using the <tt>:allow_destroy</tt>
      # option for +accepts_nested_attributes_for+:
      #
      #   class Person < ActiveRecord::Base
      #     has_many :projects
      #     accepts_nested_attributes_for :projects, allow_destroy: true
      #   end
      #
      # This will allow you to specify which models to destroy in the
      # attributes hash by adding a form element for the <tt>_destroy</tt>
      # parameter with a value that evaluates to +true+
      # (e.g. 1, '1', true, or 'true'):
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       Delete: <%= project_fields.check_box :_destroy %>
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # When a collection is used you might want to know the index of each
      # object in the array. For this purpose, the <tt>index</tt> method
      # is available in the FormBuilder object.
      #
      #   <%= form_for @person do |person_form| %>
      #     ...
      #     <%= person_form.fields_for :projects do |project_fields| %>
      #       Project #<%= project_fields.index %>
      #       ...
      #     <% end %>
      #     ...
      #   <% end %>
      #
      # Note that fields_for will automatically generate a hidden field
      # to store the ID of the record. There are circumstances where this
      # hidden field is not needed and you can pass <tt>include_id: false</tt>
      # to prevent fields_for from rendering it automatically.
      def fields_for(record_name, record_object = nil, fields_options = nil, &block)
        fields_options, record_object = record_object, nil if fields_options.nil? && record_object.is_a?(Hash) && record_object.extractable_options?
        fields_options ||= {}
        fields_options[:builder] ||= options[:builder]
        fields_options[:namespace] = options[:namespace]
        fields_options[:parent_builder] = self

        case record_name
        when String, Symbol
          if nested_attributes_association?(record_name)
            return fields_for_with_nested_attributes(record_name, record_object, fields_options, block)
          end
        else
          record_object = @template._object_for_form_builder(record_name)
          record_name   = model_name_from_record_or_class(record_object).param_key
        end

        object_name = @object_name
        index = if options.has_key?(:index)
          options[:index]
        elsif defined?(@auto_index)
          object_name = object_name.to_s.delete_suffix("[]")
          @auto_index
        end

        record_name = if index
          "#{object_name}[#{index}][#{record_name}]"
        elsif record_name.end_with?("[]")
          "#{object_name}[#{record_name[0..-3]}][#{record_object.id}]"
        else
          "#{object_name}[#{record_name}]"
        end
        fields_options[:child_index] = index

        @template.fields_for(record_name, record_object, fields_options, &block)
      end

      # See the docs for the ActionView::Helpers::FormHelper#fields helper method.
      def fields(scope = nil, model: nil, **options, &block)
        options[:allow_method_names_outside_object] = true
        options[:skip_default_ids] = !FormHelper.form_with_generates_ids

        convert_to_legacy_options(options)

        fields_for(scope || model, model, options, &block)
      end

      # Returns a label tag tailored for labelling an input field for a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). The text of label will default to the attribute name unless a translation
      # is found in the current I18n locale (through <tt>helpers.label.<modelname>.<attribute></tt>) or you specify it explicitly.
      # Additional options on the label tag can be passed as a hash with +options+. These options will be tagged
      # onto the HTML as an HTML element attribute as in the example shown, except for the <tt>:value</tt> option, which is designed to
      # target labels for radio_button tags (where the value is used in the ID of the input tag).
      #
      # ==== Examples
      #   label(:title)
      #   # => <label for="post_title">Title</label>
      #
      # You can localize your labels based on model and attribute names.
      # For example you can define the following in your locale (e.g. en.yml)
      #
      #   helpers:
      #     label:
      #       post:
      #         body: "Write your entire text here"
      #
      # Which then will result in
      #
      #   label(:body)
      #   # => <label for="post_body">Write your entire text here</label>
      #
      # Localization can also be based purely on the translation of the attribute-name
      # (if you are using ActiveRecord):
      #
      #   activerecord:
      #     attributes:
      #       post:
      #         cost: "Total cost"
      #
      # <code></code>
      #
      #   label(:cost)
      #   # => <label for="post_cost">Total cost</label>
      #
      #   label(:title, "A short title")
      #   # => <label for="post_title">A short title</label>
      #
      #   label(:title, "A short title", class: "title_label")
      #   # => <label for="post_title" class="title_label">A short title</label>
      #
      #   label(:privacy, "Public Post", value: "public")
      #   # => <label for="post_privacy_public">Public Post</label>
      #
      #   label(:cost) do |translation|
      #     content_tag(:span, translation, class: "cost_label")
      #   end
      #   # => <label for="post_cost"><span class="cost_label">Total cost</span></label>
      #
      #   label(:cost) do |builder|
      #     content_tag(:span, builder.translation, class: "cost_label")
      #   end
      #   # => <label for="post_cost"><span class="cost_label">Total cost</span></label>
      #
      #   label(:cost) do |builder|
      #     content_tag(:span, builder.translation, class: [
      #       "cost_label",
      #       ("error_label" if builder.object.errors.include?(:cost))
      #     ])
      #   end
      #   # => <label for="post_cost"><span class="cost_label error_label">Total cost</span></label>
      #
      #   label(:terms) do
      #     raw('Accept <a href="/terms">Terms</a>.')
      #   end
      #   # => <label for="post_terms">Accept <a href="/terms">Terms</a>.</label>
      def label(method, text = nil, options = {}, &block)
        @template.label(@object_name, method, text, objectify_options(options), &block)
      end

      # Returns a checkbox tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). This object must be an instance object (@object) and not a local object.
      # It's intended that +method+ returns an integer and if that integer is above zero, then the checkbox is checked.
      # Additional options on the input tag can be passed as a hash with +options+. The +checked_value+ defaults to 1
      # while the default +unchecked_value+ is set to 0 which is convenient for boolean values.
      #
      # ==== Options
      #
      # * Any standard HTML attributes for the tag can be passed in, for example +:class+.
      # * <tt>:checked</tt> - +true+ or +false+ forces the state of the checkbox to be checked or not.
      # * <tt>:include_hidden</tt> - If set to false, the auxiliary hidden field described below will not be generated.
      #
      # ==== Gotcha
      #
      # The HTML specification says unchecked check boxes are not successful, and
      # thus web browsers do not send them. Unfortunately this introduces a gotcha:
      # if an +Invoice+ model has a +paid+ flag, and in the form that edits a paid
      # invoice the user unchecks its check box, no +paid+ parameter is sent. So,
      # any mass-assignment idiom like
      #
      #   @invoice.update(params[:invoice])
      #
      # wouldn't update the flag.
      #
      # To prevent this the helper generates an auxiliary hidden field before
      # every check box. The hidden field has the same name and its
      # attributes mimic an unchecked check box.
      #
      # This way, the client either sends only the hidden field (representing
      # the check box is unchecked), or both fields. Since the HTML specification
      # says key/value pairs have to be sent in the same order they appear in the
      # form, and parameters extraction gets the last occurrence of any repeated
      # key in the query string, that works for ordinary forms.
      #
      # Unfortunately that workaround does not work when the check box goes
      # within an array-like parameter, as in
      #
      #   <%= fields_for "project[invoice_attributes][]", invoice, index: nil do |form| %>
      #     <%= form.check_box :paid %>
      #     ...
      #   <% end %>
      #
      # because parameter name repetition is precisely what \Rails seeks to distinguish
      # the elements of the array. For each item with a checked check box you
      # get an extra ghost item with only that attribute, assigned to "0".
      #
      # In that case it is preferable to either use +check_box_tag+ or to use
      # hashes instead of arrays.
      #
      # ==== Examples
      #
      #   # Let's say that @post.validated? is 1:
      #   check_box("validated")
      #   # => <input name="post[validated]" type="hidden" value="0" />
      #   #    <input checked="checked" type="checkbox" id="post_validated" name="post[validated]" value="1" />
      #
      #   # Let's say that @puppy.gooddog is "no":
      #   check_box("gooddog", {}, "yes", "no")
      #   # => <input name="puppy[gooddog]" type="hidden" value="no" />
      #   #    <input type="checkbox" id="puppy_gooddog" name="puppy[gooddog]" value="yes" />
      #
      #   # Let's say that @eula.accepted is "no":
      #   check_box("accepted", { class: 'eula_check' }, "yes", "no")
      #   # => <input name="eula[accepted]" type="hidden" value="no" />
      #   #    <input type="checkbox" class="eula_check" id="eula_accepted" name="eula[accepted]" value="yes" />
      def check_box(method, options = {}, checked_value = "1", unchecked_value = "0")
        @template.check_box(@object_name, method, objectify_options(options), checked_value, unchecked_value)
      end

      # Returns a radio button tag for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). If the current value of +method+ is +tag_value+ the
      # radio button will be checked.
      #
      # To force the radio button to be checked pass <tt>checked: true</tt> in the
      # +options+ hash. You may pass HTML options there as well.
      #
      #   # Let's say that @post.category returns "rails":
      #   radio_button("category", "rails")
      #   radio_button("category", "java")
      #   # => <input type="radio" id="post_category_rails" name="post[category]" value="rails" checked="checked" />
      #   #    <input type="radio" id="post_category_java" name="post[category]" value="java" />
      #
      #   # Let's say that @user.receive_newsletter returns "no":
      #   radio_button("receive_newsletter", "yes")
      #   radio_button("receive_newsletter", "no")
      #   # => <input type="radio" id="user_receive_newsletter_yes" name="user[receive_newsletter]" value="yes" />
      #   #    <input type="radio" id="user_receive_newsletter_no" name="user[receive_newsletter]" value="no" checked="checked" />
      def radio_button(method, tag_value, options = {})
        @template.radio_button(@object_name, method, tag_value, objectify_options(options))
      end

      # Returns a hidden input tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown.
      #
      # ==== Examples
      #   # Let's say that @signup.pass_confirm returns true:
      #   hidden_field(:pass_confirm)
      #   # => <input type="hidden" id="signup_pass_confirm" name="signup[pass_confirm]" value="true" />
      #
      #   # Let's say that @post.tag_list returns "blog, ruby":
      #   hidden_field(:tag_list)
      #   # => <input type="hidden" id="post_tag_list" name="post[tag_list]" value="blog, ruby" />
      #
      #   # Let's say that @user.token returns "abcde":
      #   hidden_field(:token)
      #   # => <input type="hidden" id="user_token" name="user[token]" value="abcde" />
      #
      def hidden_field(method, options = {})
        @emitted_hidden_id = true if method == :id
        @template.hidden_field(@object_name, method, objectify_options(options))
      end

      # Returns a file upload input tag tailored for accessing a specified attribute (identified by +method+) on an object
      # assigned to the template (identified by +object+). Additional options on the input tag can be passed as a
      # hash with +options+. These options will be tagged onto the HTML as an HTML element attribute as in the example
      # shown.
      #
      # Using this method inside a +form_with+ block will set the enclosing form's encoding to <tt>multipart/form-data</tt>.
      #
      # ==== Options
      # * Creates standard HTML attributes for the tag.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:multiple</tt> - If set to true, *in most updated browsers* the user will be allowed to select multiple files.
      # * <tt>:include_hidden</tt> - When <tt>multiple: true</tt> and <tt>include_hidden: true</tt>, the field will be prefixed with an <tt><input type="hidden"></tt> field with an empty value to support submitting an empty collection of files. Since <tt>include_hidden</tt> will default to <tt>config.active_storage.multiple_file_field_include_hidden</tt> if you don't specify <tt>include_hidden</tt>, you will need to pass <tt>include_hidden: false</tt> to prevent submitting an empty collection of files when passing <tt>multiple: true</tt>.
      # * <tt>:accept</tt> - If set to one or multiple mime-types, the user will be suggested a filter when choosing a file. You still need to set up model validations.
      #
      # ==== Examples
      #   # Let's say that @user has avatar:
      #   file_field(:avatar)
      #   # => <input type="file" id="user_avatar" name="user[avatar]" />
      #
      #   # Let's say that @post has image:
      #   file_field(:image, :multiple => true)
      #   # => <input type="file" id="post_image" name="post[image][]" multiple="multiple" />
      #
      #   # Let's say that @post has attached:
      #   file_field(:attached, accept: 'text/html')
      #   # => <input accept="text/html" type="file" id="post_attached" name="post[attached]" />
      #
      #   # Let's say that @post has image:
      #   file_field(:image, accept: 'image/png,image/gif,image/jpeg')
      #   # => <input type="file" id="post_image" name="post[image]" accept="image/png,image/gif,image/jpeg" />
      #
      #   # Let's say that @attachment has file:
      #   file_field(:file, class: 'file_input')
      #   # => <input type="file" id="attachment_file" name="attachment[file]" class="file_input" />
      def file_field(method, options = {})
        self.multipart = true
        @template.file_field(@object_name, method, objectify_options(options))
      end

      # Add the submit button for the given form. When no value is given, it checks
      # if the object is a new resource or not to create the proper label:
      #
      #   <%= form_for @post do |f| %>
      #     <%= f.submit %>
      #   <% end %>
      #
      # In the example above, if <tt>@post</tt> is a new record, it will use "Create Post" as
      # submit button label; otherwise, it uses "Update Post".
      #
      # Those labels can be customized using I18n under the +helpers.submit+ key and using
      # <tt>%{model}</tt> for translation interpolation:
      #
      #   en:
      #     helpers:
      #       submit:
      #         create: "Create a %{model}"
      #         update: "Confirm changes to %{model}"
      #
      # It also searches for a key specific to the given object:
      #
      #   en:
      #     helpers:
      #       submit:
      #         post:
      #           create: "Add %{model}"
      #
      def submit(value = nil, options = {})
        value, options = nil, value if value.is_a?(Hash)
        value ||= submit_default_value
        @template.submit_tag(value, options)
      end

      # Add the submit button for the given form. When no value is given, it checks
      # if the object is a new resource or not to create the proper label:
      #
      #   <%= form_for @post do |f| %>
      #     <%= f.button %>
      #   <% end %>
      #
      # In the example above, if <tt>@post</tt> is a new record, it will use "Create Post" as
      # button label; otherwise, it uses "Update Post".
      #
      # Those labels can be customized using I18n under the +helpers.submit+ key
      # (the same as submit helper) and using <tt>%{model}</tt> for translation interpolation:
      #
      #   en:
      #     helpers:
      #       submit:
      #         create: "Create a %{model}"
      #         update: "Confirm changes to %{model}"
      #
      # It also searches for a key specific to the given object:
      #
      #   en:
      #     helpers:
      #       submit:
      #         post:
      #           create: "Add %{model}"
      #
      # ==== Examples
      #   button("Create post")
      #   # => <button name='button' type='submit'>Create post</button>
      #
      #   button(:draft, value: true)
      #   # => <button id="post_draft" name="post[draft]" value="true" type="submit">Create post</button>
      #
      #   button do
      #     content_tag(:strong, 'Ask me!')
      #   end
      #   # => <button name='button' type='submit'>
      #   #      <strong>Ask me!</strong>
      #   #    </button>
      #
      #   button do |text|
      #     content_tag(:strong, text)
      #   end
      #   # => <button name='button' type='submit'>
      #   #      <strong>Create post</strong>
      #   #    </button>
      #
      #   button(:draft, value: true) do
      #     content_tag(:strong, "Save as draft")
      #   end
      #   # =>  <button id="post_draft" name="post[draft]" value="true" type="submit">
      #   #       <strong>Save as draft</strong>
      #   #     </button>
      #
      def button(value = nil, options = {}, &block)
        case value
        when Hash
          value, options = nil, value
        when Symbol
          value, options = nil, { name: field_name(value), id: field_id(value) }.merge!(options.to_h)
        end
        value ||= submit_default_value

        if block_given?
          value = @template.capture { yield(value) }
        end

        formmethod = options[:formmethod]
        if formmethod.present? && !/post|get/i.match?(formmethod) && !options.key?(:name) && !options.key?(:value)
          options.merge! formmethod: :post, name: "_method", value: formmethod
        end

        @template.button_tag(value, options)
      end

      def emitted_hidden_id? # :nodoc:
        @emitted_hidden_id ||= nil
      end

      private
        def objectify_options(options)
          result = @default_options.merge(options)
          result[:object] = @object
          result
        end

        def submit_default_value
          object = convert_to_model(@object)
          key    = object ? (object.persisted? ? :update : :create) : :submit

          model = if object.respond_to?(:model_name)
            object.model_name.human
          else
            @object_name.to_s.humanize
          end

          defaults = []
          # Object is a model and it is not overwritten by as and scope option.
          if object.respond_to?(:model_name) && object_name.to_s == model.downcase
            defaults << :"helpers.submit.#{object.model_name.i18n_key}.#{key}"
          else
            defaults << :"helpers.submit.#{object_name}.#{key}"
          end
          defaults << :"helpers.submit.#{key}"
          defaults << "#{key.to_s.humanize} #{model}"

          I18n.t(defaults.shift, model: model, default: defaults)
        end

        def nested_attributes_association?(association_name)
          @object.respond_to?("#{association_name}_attributes=")
        end

        def fields_for_with_nested_attributes(association_name, association, options, block)
          name = "#{object_name}[#{association_name}_attributes]"
          association = convert_to_model(association)

          if association.respond_to?(:persisted?)
            association = [association] if @object.public_send(association_name).respond_to?(:to_ary)
          elsif !association.respond_to?(:to_ary)
            association = @object.public_send(association_name)
          end

          if association.respond_to?(:to_ary)
            explicit_child_index = options[:child_index]
            output = ActiveSupport::SafeBuffer.new
            association.each do |child|
              if explicit_child_index
                options[:child_index] = explicit_child_index.call if explicit_child_index.respond_to?(:call)
              else
                options[:child_index] = nested_child_index(name)
              end
              if content = fields_for_nested_model("#{name}[#{options[:child_index]}]", child, options, block)
                output << content
              end
            end
            output
          elsif association
            fields_for_nested_model(name, association, options, block)
          end
        end

        def fields_for_nested_model(name, object, fields_options, block)
          object = convert_to_model(object)
          emit_hidden_id = object.persisted? && fields_options.fetch(:include_id) {
            options.fetch(:include_id, true)
          }

          @template.fields_for(name, object, fields_options) do |f|
            output = @template.capture(f, &block)
            output.concat f.hidden_field(:id) if output && emit_hidden_id && !f.emitted_hidden_id?
            output
          end
        end

        def nested_child_index(name)
          @nested_child_index[name] ||= -1
          @nested_child_index[name] += 1
        end

        def convert_to_legacy_options(options)
          if options.key?(:skip_id)
            options[:include_id] = !options.delete(:skip_id)
          end
        end
    end
  end

  ActiveSupport.on_load(:action_view) do
    cattr_accessor :default_form_builder, instance_writer: false, instance_reader: false, default: ::ActionView::Helpers::FormBuilder
  end
end


// --- rb_query_methods.rb ---
# frozen_string_literal: true

require "active_record/relation/from_clause"
require "active_record/relation/query_attribute"
require "active_record/relation/where_clause"
require "active_support/core_ext/array/wrap"

module ActiveRecord
  module QueryMethods
    include ActiveModel::ForbiddenAttributesProtection

    # +WhereChain+ objects act as placeholder for queries in which +where+ does not have any parameter.
    # In this case, +where+ can be chained to return a new relation.
    class WhereChain
      def initialize(scope) # :nodoc:
        @scope = scope
      end

      # Returns a new relation expressing WHERE + NOT condition according to
      # the conditions in the arguments.
      #
      # #not accepts conditions as a string, array, or hash. See QueryMethods#where for
      # more details on each format.
      #
      #    User.where.not("name = 'Jon'")
      #    # SELECT * FROM users WHERE NOT (name = 'Jon')
      #
      #    User.where.not(["name = ?", "Jon"])
      #    # SELECT * FROM users WHERE NOT (name = 'Jon')
      #
      #    User.where.not(name: "Jon")
      #    # SELECT * FROM users WHERE name != 'Jon'
      #
      #    User.where.not(name: nil)
      #    # SELECT * FROM users WHERE name IS NOT NULL
      #
      #    User.where.not(name: %w(Ko1 Nobu))
      #    # SELECT * FROM users WHERE name NOT IN ('Ko1', 'Nobu')
      #
      #    User.where.not(name: "Jon", role: "admin")
      #    # SELECT * FROM users WHERE NOT (name = 'Jon' AND role = 'admin')
      #
      # If there is a non-nil condition on a nullable column in the hash condition, the records that have
      # nil values on the nullable column won't be returned.
      #    User.create!(nullable_country: nil)
      #    User.where.not(nullable_country: "UK")
      #    # SELECT * FROM users WHERE NOT (nullable_country = 'UK')
      #    # => []
      def not(opts, *rest)
        where_clause = @scope.send(:build_where_clause, opts, rest)

        @scope.where_clause += where_clause.invert

        @scope
      end

      # Returns a new relation with joins and where clause to identify
      # associated relations.
      #
      # For example, posts that are associated to a related author:
      #
      #    Post.where.associated(:author)
      #    # SELECT "posts".* FROM "posts"
      #    # INNER JOIN "authors" ON "authors"."id" = "posts"."author_id"
      #    # WHERE "authors"."id" IS NOT NULL
      #
      # Additionally, multiple relations can be combined. This will return posts
      # associated to both an author and any comments:
      #
      #    Post.where.associated(:author, :comments)
      #    # SELECT "posts".* FROM "posts"
      #    # INNER JOIN "authors" ON "authors"."id" = "posts"."author_id"
      #    # INNER JOIN "comments" ON "comments"."post_id" = "posts"."id"
      #    # WHERE "authors"."id" IS NOT NULL AND "comments"."id" IS NOT NULL
      def associated(*associations)
        associations.each do |association|
          reflection = scope_association_reflection(association)
          @scope.joins!(association)
          if reflection.options[:class_name]
            self.not(association => { reflection.association_primary_key => nil })
          else
            self.not(reflection.table_name => { reflection.association_primary_key => nil })
          end
        end

        @scope
      end

      # Returns a new relation with left outer joins and where clause to identify
      # missing relations.
      #
      # For example, posts that are missing a related author:
      #
      #    Post.where.missing(:author)
      #    # SELECT "posts".* FROM "posts"
      #    # LEFT OUTER JOIN "authors" ON "authors"."id" = "posts"."author_id"
      #    # WHERE "authors"."id" IS NULL
      #
      # Additionally, multiple relations can be combined. This will return posts
      # that are missing both an author and any comments:
      #
      #    Post.where.missing(:author, :comments)
      #    # SELECT "posts".* FROM "posts"
      #    # LEFT OUTER JOIN "authors" ON "authors"."id" = "posts"."author_id"
      #    # LEFT OUTER JOIN "comments" ON "comments"."post_id" = "posts"."id"
      #    # WHERE "authors"."id" IS NULL AND "comments"."id" IS NULL
      def missing(*associations)
        associations.each do |association|
          reflection = scope_association_reflection(association)
          @scope.left_outer_joins!(association)
          if reflection.options[:class_name]
            @scope.where!(association => { reflection.association_primary_key => nil })
          else
            @scope.where!(reflection.table_name => { reflection.association_primary_key => nil })
          end
        end

        @scope
      end

      private
        def scope_association_reflection(association)
          reflection = @scope.klass._reflect_on_association(association)
          unless reflection
            raise ArgumentError.new("An association named `:#{association}` does not exist on the model `#{@scope.name}`.")
          end
          reflection
        end
    end

    # A wrapper to distinguish CTE joins from other nodes.
    class CTEJoin # :nodoc:
      attr_reader :name

      def initialize(name)
        @name = name
      end
    end

    FROZEN_EMPTY_ARRAY = [].freeze
    FROZEN_EMPTY_HASH = {}.freeze

    Relation::VALUE_METHODS.each do |name|
      method_name, default =
        case name
        when *Relation::MULTI_VALUE_METHODS
          ["#{name}_values", "FROZEN_EMPTY_ARRAY"]
        when *Relation::SINGLE_VALUE_METHODS
          ["#{name}_value", name == :create_with ? "FROZEN_EMPTY_HASH" : "nil"]
        when *Relation::CLAUSE_METHODS
          ["#{name}_clause", name == :from ? "Relation::FromClause.empty" : "Relation::WhereClause.empty"]
        end

      class_eval <<-CODE, __FILE__, __LINE__ + 1
        def #{method_name}                     # def includes_values
          @values.fetch(:#{name}, #{default})  #   @values.fetch(:includes, FROZEN_EMPTY_ARRAY)
        end                                    # end

        def #{method_name}=(value)             # def includes_values=(value)
          assert_mutability!                   #   assert_mutability!
          @values[:#{name}] = value            #   @values[:includes] = value
        end                                    # end
      CODE
    end

    alias extensions extending_values

    # Specify associations +args+ to be eager loaded to prevent N + 1 queries.
    # A separate query is performed for each association, unless a join is
    # required by conditions.
    #
    # For example:
    #
    #   users = User.includes(:address).limit(5)
    #   users.each do |user|
    #     user.address.city
    #   end
    #
    #   # SELECT "users".* FROM "users" LIMIT 5
    #   # SELECT "addresses".* FROM "addresses" WHERE "addresses"."id" IN (1,2,3,4,5)
    #
    # Instead of loading the 5 addresses with 5 separate queries, all addresses
    # are loaded with a single query.
    #
    # Loading the associations in a separate query will often result in a
    # performance improvement over a simple join, as a join can result in many
    # rows that contain redundant data and it performs poorly at scale.
    #
    # You can also specify multiple associations. Each association will result
    # in an additional query:
    #
    #   User.includes(:address, :friends).to_a
    #   # SELECT "users".* FROM "users"
    #   # SELECT "addresses".* FROM "addresses" WHERE "addresses"."id" IN (1,2,3,4,5)
    #   # SELECT "friends".* FROM "friends" WHERE "friends"."user_id" IN (1,2,3,4,5)
    #
    # Loading nested associations is possible using a Hash:
    #
    #   User.includes(:address, friends: [:address, :followers])
    #
    # === Conditions
    #
    # If you want to add string conditions to your included models, you'll have
    # to explicitly reference them. For example:
    #
    #   User.includes(:posts).where('posts.name = ?', 'example').to_a
    #
    # Will throw an error, but this will work:
    #
    #   User.includes(:posts).where('posts.name = ?', 'example').references(:posts).to_a
    #   # SELECT "users"."id" AS t0_r0, ... FROM "users"
    #   #   LEFT OUTER JOIN "posts" ON "posts"."user_id" = "users"."id"
    #   #   WHERE "posts"."name" = ?  [["name", "example"]]
    #
    # As the <tt>LEFT OUTER JOIN</tt> already contains the posts, the second query for
    # the posts is no longer performed.
    #
    # Note that #includes works with association names while #references needs
    # the actual table name.
    #
    # If you pass the conditions via a Hash, you don't need to call #references
    # explicitly, as #where references the tables for you. For example, this
    # will work correctly:
    #
    #   User.includes(:posts).where(posts: { name: 'example' })
    #
    # NOTE: Conditions affect both sides of an association. For example, the
    # above code will return only users that have a post named "example",
    # <em>and will only include posts named "example"</em>, even when a
    # matching user has other additional posts.
    def includes(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.includes!(*args)
    end

    def includes!(*args) # :nodoc:
      self.includes_values |= args
      self
    end

    # Specify associations +args+ to be eager loaded using a <tt>LEFT OUTER JOIN</tt>.
    # Performs a single query joining all specified associations. For example:
    #
    #   users = User.eager_load(:address).limit(5)
    #   users.each do |user|
    #     user.address.city
    #   end
    #
    #   # SELECT "users"."id" AS t0_r0, "users"."name" AS t0_r1, ... FROM "users"
    #   #   LEFT OUTER JOIN "addresses" ON "addresses"."id" = "users"."address_id"
    #   #   LIMIT 5
    #
    # Instead of loading the 5 addresses with 5 separate queries, all addresses
    # are loaded with a single joined query.
    #
    # Loading multiple and nested associations is possible using Hashes and Arrays,
    # similar to #includes:
    #
    #   User.eager_load(:address, friends: [:address, :followers])
    #   # SELECT "users"."id" AS t0_r0, "users"."name" AS t0_r1, ... FROM "users"
    #   #   LEFT OUTER JOIN "addresses" ON "addresses"."id" = "users"."address_id"
    #   #   LEFT OUTER JOIN "friends" ON "friends"."user_id" = "users"."id"
    #   #   ...
    #
    # NOTE: Loading the associations in a join can result in many rows that
    # contain redundant data and it performs poorly at scale.
    def eager_load(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.eager_load!(*args)
    end

    def eager_load!(*args) # :nodoc:
      self.eager_load_values |= args
      self
    end

    # Specify associations +args+ to be eager loaded using separate queries.
    # A separate query is performed for each association.
    #
    #   users = User.preload(:address).limit(5)
    #   users.each do |user|
    #     user.address.city
    #   end
    #
    #   # SELECT "users".* FROM "users" LIMIT 5
    #   # SELECT "addresses".* FROM "addresses" WHERE "addresses"."id" IN (1,2,3,4,5)
    #
    # Instead of loading the 5 addresses with 5 separate queries, all addresses
    # are loaded with a separate query.
    #
    # Loading multiple and nested associations is possible using Hashes and Arrays,
    # similar to #includes:
    #
    #   User.preload(:address, friends: [:address, :followers])
    #   # SELECT "users".* FROM "users"
    #   # SELECT "addresses".* FROM "addresses" WHERE "addresses"."id" IN (1,2,3,4,5)
    #   # SELECT "friends".* FROM "friends" WHERE "friends"."user_id" IN (1,2,3,4,5)
    #   # SELECT ...
    def preload(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.preload!(*args)
    end

    def preload!(*args) # :nodoc:
      self.preload_values |= args
      self
    end

    # Extracts a named +association+ from the relation. The named association is first preloaded,
    # then the individual association records are collected from the relation. Like so:
    #
    #   account.memberships.extract_associated(:user)
    #   # => Returns collection of User records
    #
    # This is short-hand for:
    #
    #   account.memberships.preload(:user).collect(&:user)
    def extract_associated(association)
      preload(association).collect(&association)
    end

    # Use to indicate that the given +table_names+ are referenced by an SQL string,
    # and should therefore be +JOIN+ed in any query rather than loaded separately.
    # This method only works in conjunction with #includes.
    # See #includes for more details.
    #
    #   User.includes(:posts).where("posts.name = 'foo'")
    #   # Doesn't JOIN the posts table, resulting in an error.
    #
    #   User.includes(:posts).where("posts.name = 'foo'").references(:posts)
    #   # Query now knows the string references posts, so adds a JOIN
    def references(*table_names)
      check_if_method_has_arguments!(__callee__, table_names)
      spawn.references!(*table_names)
    end

    def references!(*table_names) # :nodoc:
      self.references_values |= table_names
      self
    end

    # Works in two unique ways.
    #
    # First: takes a block so it can be used just like <tt>Array#select</tt>.
    #
    #   Model.all.select { |m| m.field == value }
    #
    # This will build an array of objects from the database for the scope,
    # converting them into an array and iterating through them using
    # <tt>Array#select</tt>.
    #
    # Second: Modifies the SELECT statement for the query so that only certain
    # fields are retrieved:
    #
    #   Model.select(:field)
    #   # => [#<Model id: nil, field: "value">]
    #
    # Although in the above example it looks as though this method returns an
    # array, it actually returns a relation object and can have other query
    # methods appended to it, such as the other methods in ActiveRecord::QueryMethods.
    #
    # The argument to the method can also be an array of fields.
    #
    #   Model.select(:field, :other_field, :and_one_more)
    #   # => [#<Model id: nil, field: "value", other_field: "value", and_one_more: "value">]
    #
    # The argument also can be a hash of fields and aliases.
    #
    #   Model.select(models: { field: :alias, other_field: :other_alias })
    #   # => [#<Model id: nil, alias: "value", other_alias: "value">]
    #
    #   Model.select(models: [:field, :other_field])
    #   # => [#<Model id: nil, field: "value", other_field: "value">]
    #
    # You can also use one or more strings, which will be used unchanged as SELECT fields.
    #
    #   Model.select('field AS field_one', 'other_field AS field_two')
    #   # => [#<Model id: nil, field_one: "value", field_two: "value">]
    #
    # If an alias was specified, it will be accessible from the resulting objects:
    #
    #   Model.select('field AS field_one').first.field_one
    #   # => "value"
    #
    # Accessing attributes of an object that do not have fields retrieved by a select
    # except +id+ will throw ActiveModel::MissingAttributeError:
    #
    #   Model.select(:field).first.other_field
    #   # => ActiveModel::MissingAttributeError: missing attribute 'other_field' for Model
    def select(*fields)
      if block_given?
        if fields.any?
          raise ArgumentError, "`select' with block doesn't take arguments."
        end

        return super()
      end

      check_if_method_has_arguments!(__callee__, fields, "Call `select' with at least one field.")

      fields = process_select_args(fields)
      spawn._select!(*fields)
    end

    def _select!(*fields) # :nodoc:
      self.select_values |= fields
      self
    end

    # Add a Common Table Expression (CTE) that you can then reference within another SELECT statement.
    #
    # Note: CTE's are only supported in MySQL for versions 8.0 and above. You will not be able to
    # use CTE's with MySQL 5.7.
    #
    #   Post.with(posts_with_tags: Post.where("tags_count > ?", 0))
    #   # => ActiveRecord::Relation
    #   # WITH posts_with_tags AS (
    #   #   SELECT * FROM posts WHERE (tags_count > 0)
    #   # )
    #   # SELECT * FROM posts
    #
    # Once you define Common Table Expression you can use custom +FROM+ value or +JOIN+ to reference it.
    #
    #   Post.with(posts_with_tags: Post.where("tags_count > ?", 0)).from("posts_with_tags AS posts")
    #   # => ActiveRecord::Relation
    #   # WITH posts_with_tags AS (
    #   #  SELECT * FROM posts WHERE (tags_count > 0)
    #   # )
    #   # SELECT * FROM posts_with_tags AS posts
    #
    #   Post.with(posts_with_tags: Post.where("tags_count > ?", 0)).joins("JOIN posts_with_tags ON posts_with_tags.id = posts.id")
    #   # => ActiveRecord::Relation
    #   # WITH posts_with_tags AS (
    #   #   SELECT * FROM posts WHERE (tags_count > 0)
    #   # )
    #   # SELECT * FROM posts JOIN posts_with_tags ON posts_with_tags.id = posts.id
    #
    # It is recommended to pass a query as ActiveRecord::Relation. If that is not possible
    # and you have verified it is safe for the database, you can pass it as SQL literal
    # using +Arel+.
    #
    #   Post.with(popular_posts: Arel.sql("... complex sql to calculate posts popularity ..."))
    #
    # Great caution should be taken to avoid SQL injection vulnerabilities. This method should not
    # be used with unsafe values that include unsanitized input.
    #
    # To add multiple CTEs just pass multiple key-value pairs
    #
    #   Post.with(
    #     posts_with_comments: Post.where("comments_count > ?", 0),
    #     posts_with_tags: Post.where("tags_count > ?", 0)
    #   )
    #
    # or chain multiple +.with+ calls
    #
    #   Post
    #     .with(posts_with_comments: Post.where("comments_count > ?", 0))
    #     .with(posts_with_tags: Post.where("tags_count > ?", 0))
    def with(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.with!(*args)
    end

    # Like #with, but modifies relation in place.
    def with!(*args) # :nodoc:
      self.with_values += args
      self
    end

    # Allows you to change a previously set select statement.
    #
    #   Post.select(:title, :body)
    #   # SELECT `posts`.`title`, `posts`.`body` FROM `posts`
    #
    #   Post.select(:title, :body).reselect(:created_at)
    #   # SELECT `posts`.`created_at` FROM `posts`
    #
    # This is short-hand for <tt>unscope(:select).select(fields)</tt>.
    # Note that we're unscoping the entire select statement.
    def reselect(*args)
      check_if_method_has_arguments!(__callee__, args)
      args = process_select_args(args)
      spawn.reselect!(*args)
    end

    # Same as #reselect but operates on relation in-place instead of copying.
    def reselect!(*args) # :nodoc:
      self.select_values = args
      self
    end

    # Allows to specify a group attribute:
    #
    #   User.group(:name)
    #   # SELECT "users".* FROM "users" GROUP BY name
    #
    # Returns an array with distinct records based on the +group+ attribute:
    #
    #   User.select([:id, :name])
    #   # => [#<User id: 1, name: "Oscar">, #<User id: 2, name: "Oscar">, #<User id: 3, name: "Foo">]
    #
    #   User.group(:name)
    #   # => [#<User id: 3, name: "Foo", ...>, #<User id: 2, name: "Oscar", ...>]
    #
    #   User.group('name AS grouped_name, age')
    #   # => [#<User id: 3, name: "Foo", age: 21, ...>, #<User id: 2, name: "Oscar", age: 21, ...>, #<User id: 5, name: "Foo", age: 23, ...>]
    #
    # Passing in an array of attributes to group by is also supported.
    #
    #   User.select([:id, :first_name]).group(:id, :first_name).first(3)
    #   # => [#<User id: 1, first_name: "Bill">, #<User id: 2, first_name: "Earl">, #<User id: 3, first_name: "Beto">]
    def group(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.group!(*args)
    end

    def group!(*args) # :nodoc:
      self.group_values += args
      self
    end

    # Allows you to change a previously set group statement.
    #
    #   Post.group(:title, :body)
    #   # SELECT `posts`.`*` FROM `posts` GROUP BY `posts`.`title`, `posts`.`body`
    #
    #   Post.group(:title, :body).regroup(:title)
    #   # SELECT `posts`.`*` FROM `posts` GROUP BY `posts`.`title`
    #
    # This is short-hand for <tt>unscope(:group).group(fields)</tt>.
    # Note that we're unscoping the entire group statement.
    def regroup(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.regroup!(*args)
    end

    # Same as #regroup but operates on relation in-place instead of copying.
    def regroup!(*args) # :nodoc:
      self.group_values = args
      self
    end

    # Applies an <code>ORDER BY</code> clause to a query.
    #
    # #order accepts arguments in one of several formats.
    #
    # === symbols
    #
    # The symbol represents the name of the column you want to order the results by.
    #
    #   User.order(:name)
    #   # SELECT "users".* FROM "users" ORDER BY "users"."name" ASC
    #
    # By default, the order is ascending. If you want descending order, you can
    # map the column name symbol to +:desc+.
    #
    #   User.order(email: :desc)
    #   # SELECT "users".* FROM "users" ORDER BY "users"."email" DESC
    #
    # Multiple columns can be passed this way, and they will be applied in the order specified.
    #
    #   User.order(:name, email: :desc)
    #   # SELECT "users".* FROM "users" ORDER BY "users"."name" ASC, "users"."email" DESC
    #
    # === strings
    #
    # Strings are passed directly to the database, allowing you to specify
    # simple SQL expressions.
    #
    # This could be a source of SQL injection, so only strings composed of plain
    # column names and simple <code>function(column_name)</code> expressions
    # with optional +ASC+/+DESC+ modifiers are allowed.
    #
    #   User.order('name')
    #   # SELECT "users".* FROM "users" ORDER BY name
    #
    #   User.order('name DESC')
    #   # SELECT "users".* FROM "users" ORDER BY name DESC
    #
    #   User.order('name DESC, email')
    #   # SELECT "users".* FROM "users" ORDER BY name DESC, email
    #
    # === Arel
    #
    # If you need to pass in complicated expressions that you have verified
    # are safe for the database, you can use Arel.
    #
    #   User.order(Arel.sql('end_date - start_date'))
    #   # SELECT "users".* FROM "users" ORDER BY end_date - start_date
    #
    # Custom query syntax, like JSON columns for PostgreSQL, is supported in this way.
    #
    #   User.order(Arel.sql("payload->>'kind'"))
    #   # SELECT "users".* FROM "users" ORDER BY payload->>'kind'
    def order(*args)
      check_if_method_has_arguments!(__callee__, args) do
        sanitize_order_arguments(args)
      end
      spawn.order!(*args)
    end

    # Same as #order but operates on relation in-place instead of copying.
    def order!(*args) # :nodoc:
      preprocess_order_args(args) unless args.empty?
      self.order_values |= args
      self
    end

    # Allows to specify an order by a specific set of values.
    #
    #   User.in_order_of(:id, [1, 5, 3])
    #   # SELECT "users".* FROM "users"
    #   #   WHERE "users"."id" IN (1, 5, 3)
    #   #   ORDER BY CASE
    #   #     WHEN "users"."id" = 1 THEN 1
    #   #     WHEN "users"."id" = 5 THEN 2
    #   #     WHEN "users"."id" = 3 THEN 3
    #   #   END ASC
    #
    def in_order_of(column, values)
      klass.disallow_raw_sql!([column], permit: connection.column_name_with_order_matcher)
      return spawn.none! if values.empty?

      references = column_references([column])
      self.references_values |= references unless references.empty?

      values = values.map { |value| type_caster.type_cast_for_database(column, value) }
      arel_column = column.is_a?(Arel::Nodes::SqlLiteral) ? column : order_column(column.to_s)

      where_clause =
        if values.include?(nil)
          arel_column.in(values.compact).or(arel_column.eq(nil))
        else
          arel_column.in(values)
        end

      spawn
        .order!(build_case_for_value_position(arel_column, values))
        .where!(where_clause)
    end

    # Replaces any existing order defined on the relation with the specified order.
    #
    #   User.order('email DESC').reorder('id ASC') # generated SQL has 'ORDER BY id ASC'
    #
    # Subsequent calls to order on the same relation will be appended. For example:
    #
    #   User.order('email DESC').reorder('id ASC').order('name ASC')
    #
    # generates a query with <tt>ORDER BY id ASC, name ASC</tt>.
    def reorder(*args)
      check_if_method_has_arguments!(__callee__, args) do
        sanitize_order_arguments(args)
      end
      spawn.reorder!(*args)
    end

    # Same as #reorder but operates on relation in-place instead of copying.
    def reorder!(*args) # :nodoc:
      preprocess_order_args(args)
      args.uniq!
      self.reordering_value = true
      self.order_values = args
      self
    end

    VALID_UNSCOPING_VALUES = Set.new([:where, :select, :group, :order, :lock,
                                     :limit, :offset, :joins, :left_outer_joins, :annotate,
                                     :includes, :eager_load, :preload, :from, :readonly,
                                     :having, :optimizer_hints])

    # Removes an unwanted relation that is already defined on a chain of relations.
    # This is useful when passing around chains of relations and would like to
    # modify the relations without reconstructing the entire chain.
    #
    #   User.order('email DESC').unscope(:order) == User.all
    #
    # The method arguments are symbols which correspond to the names of the methods
    # which should be unscoped. The valid arguments are given in VALID_UNSCOPING_VALUES.
    # The method can also be called with multiple arguments. For example:
    #
    #   User.order('email DESC').select('id').where(name: "John")
    #       .unscope(:order, :select, :where) == User.all
    #
    # One can additionally pass a hash as an argument to unscope specific +:where+ values.
    # This is done by passing a hash with a single key-value pair. The key should be
    # +:where+ and the value should be the where value to unscope. For example:
    #
    #   User.where(name: "John", active: true).unscope(where: :name)
    #       == User.where(active: true)
    #
    # This method is similar to #except, but unlike
    # #except, it persists across merges:
    #
    #   User.order('email').merge(User.except(:order))
    #       == User.order('email')
    #
    #   User.order('email').merge(User.unscope(:order))
    #       == User.all
    #
    # This means it can be used in association definitions:
    #
    #   has_many :comments, -> { unscope(where: :trashed) }
    #
    def unscope(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.unscope!(*args)
    end

    def unscope!(*args) # :nodoc:
      self.unscope_values += args

      args.each do |scope|
        case scope
        when Symbol
          scope = :left_outer_joins if scope == :left_joins
          if !VALID_UNSCOPING_VALUES.include?(scope)
            raise ArgumentError, "Called unscope() with invalid unscoping argument ':#{scope}'. Valid arguments are :#{VALID_UNSCOPING_VALUES.to_a.join(", :")}."
          end
          assert_mutability!
          @values.delete(scope)
        when Hash
          scope.each do |key, target_value|
            if key != :where
              raise ArgumentError, "Hash arguments in .unscope(*args) must have :where as the key."
            end

            target_values = resolve_arel_attributes(Array.wrap(target_value))
            self.where_clause = where_clause.except(*target_values)
          end
        else
          raise ArgumentError, "Unrecognized scoping: #{args.inspect}. Use .unscope(where: :attribute_name) or .unscope(:order), for example."
        end
      end

      self
    end

    # Performs JOINs on +args+. The given symbol(s) should match the name of
    # the association(s).
    #
    #   User.joins(:posts)
    #   # SELECT "users".*
    #   # FROM "users"
    #   # INNER JOIN "posts" ON "posts"."user_id" = "users"."id"
    #
    # Multiple joins:
    #
    #   User.joins(:posts, :account)
    #   # SELECT "users".*
    #   # FROM "users"
    #   # INNER JOIN "posts" ON "posts"."user_id" = "users"."id"
    #   # INNER JOIN "accounts" ON "accounts"."id" = "users"."account_id"
    #
    # Nested joins:
    #
    #   User.joins(posts: [:comments])
    #   # SELECT "users".*
    #   # FROM "users"
    #   # INNER JOIN "posts" ON "posts"."user_id" = "users"."id"
    #   # INNER JOIN "comments" ON "comments"."post_id" = "posts"."id"
    #
    # You can use strings in order to customize your joins:
    #
    #   User.joins("LEFT JOIN bookmarks ON bookmarks.bookmarkable_type = 'Post' AND bookmarks.user_id = users.id")
    #   # SELECT "users".* FROM "users" LEFT JOIN bookmarks ON bookmarks.bookmarkable_type = 'Post' AND bookmarks.user_id = users.id
    def joins(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.joins!(*args)
    end

    def joins!(*args) # :nodoc:
      self.joins_values |= args
      self
    end

    # Performs LEFT OUTER JOINs on +args+:
    #
    #   User.left_outer_joins(:posts)
    #   # SELECT "users".* FROM "users" LEFT OUTER JOIN "posts" ON "posts"."user_id" = "users"."id"
    #
    def left_outer_joins(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.left_outer_joins!(*args)
    end
    alias :left_joins :left_outer_joins

    def left_outer_joins!(*args) # :nodoc:
      self.left_outer_joins_values |= args
      self
    end

    # Returns a new relation, which is the result of filtering the current relation
    # according to the conditions in the arguments.
    #
    # #where accepts conditions in one of several formats. In the examples below, the resulting
    # SQL is given as an illustration; the actual query generated may be different depending
    # on the database adapter.
    #
    # === \String
    #
    # A single string, without additional arguments, is passed to the query
    # constructor as an SQL fragment, and used in the where clause of the query.
    #
    #    Client.where("orders_count = '2'")
    #    # SELECT * from clients where orders_count = '2';
    #
    # Note that building your own string from user input may expose your application
    # to injection attacks if not done properly. As an alternative, it is recommended
    # to use one of the following methods.
    #
    # === \Array
    #
    # If an array is passed, then the first element of the array is treated as a template, and
    # the remaining elements are inserted into the template to generate the condition.
    # Active Record takes care of building the query to avoid injection attacks, and will
    # convert from the ruby type to the database type where needed. Elements are inserted
    # into the string in the order in which they appear.
    #
    #   User.where(["name = ? and email = ?", "Joe", "joe@example.com"])
    #   # SELECT * FROM users WHERE name = 'Joe' AND email = 'joe@example.com';
    #
    # Alternatively, you can use named placeholders in the template, and pass a hash as the
    # second element of the array. The names in the template are replaced with the corresponding
    # values from the hash.
    #
    #   User.where(["name = :name and email = :email", { name: "Joe", email: "joe@example.com" }])
    #   # SELECT * FROM users WHERE name = 'Joe' AND email = 'joe@example.com';
    #
    # This can make for more readable code in complex queries.
    #
    # Lastly, you can use sprintf-style % escapes in the template. This works slightly differently
    # than the previous methods; you are responsible for ensuring that the values in the template
    # are properly quoted. The values are passed to the connector for quoting, but the caller
    # is responsible for ensuring they are enclosed in quotes in the resulting SQL. After quoting,
    # the values are inserted using the same escapes as the Ruby core method +Kernel::sprintf+.
    #
    #   User.where(["name = '%s' and email = '%s'", "Joe", "joe@example.com"])
    #   # SELECT * FROM users WHERE name = 'Joe' AND email = 'joe@example.com';
    #
    # If #where is called with multiple arguments, these are treated as if they were passed as
    # the elements of a single array.
    #
    #   User.where("name = :name and email = :email", { name: "Joe", email: "joe@example.com" })
    #   # SELECT * FROM users WHERE name = 'Joe' AND email = 'joe@example.com';
    #
    # When using strings to specify conditions, you can use any operator available from
    # the database. While this provides the most flexibility, you can also unintentionally introduce
    # dependencies on the underlying database. If your code is intended for general consumption,
    # test with multiple database backends.
    #
    # === \Hash
    #
    # #where will also accept a hash condition, in which the keys are fields and the values
    # are values to be searched for.
    #
    # Fields can be symbols or strings. Values can be single values, arrays, or ranges.
    #
    #    User.where(name: "Joe", email: "joe@example.com")
    #    # SELECT * FROM users WHERE name = 'Joe' AND email = 'joe@example.com'
    #
    #    User.where(name: ["Alice", "Bob"])
    #    # SELECT * FROM users WHERE name IN ('Alice', 'Bob')
    #
    #    User.where(created_at: (Time.now.midnight - 1.day)..Time.now.midnight)
    #    # SELECT * FROM users WHERE (created_at BETWEEN '2012-06-09 07:00:00.000000' AND '2012-06-10 07:00:00.000000')
    #
    # In the case of a belongs_to relationship, an association key can be used
    # to specify the model if an ActiveRecord object is used as the value.
    #
    #    author = Author.find(1)
    #
    #    # The following queries will be equivalent:
    #    Post.where(author: author)
    #    Post.where(author_id: author)
    #
    # This also works with polymorphic belongs_to relationships:
    #
    #    treasure = Treasure.create(name: 'gold coins')
    #    treasure.price_estimates << PriceEstimate.create(price: 125)
    #
    #    # The following queries will be equivalent:
    #    PriceEstimate.where(estimate_of: treasure)
    #    PriceEstimate.where(estimate_of_type: 'Treasure', estimate_of_id: treasure)
    #
    # Hash conditions may also be specified in a tuple-like syntax. Hash keys may be
    # an array of columns with an array of tuples as values.
    #
    #   Article.where([:author_id, :id] => [[15, 1], [15, 2]])
    #   # SELECT * FROM articles WHERE author_id = 15 AND id = 1 OR author_id = 15 AND id = 2
    #
    # === Joins
    #
    # If the relation is the result of a join, you may create a condition which uses any of the
    # tables in the join. For string and array conditions, use the table name in the condition.
    #
    #    User.joins(:posts).where("posts.created_at < ?", Time.now)
    #
    # For hash conditions, you can either use the table name in the key, or use a sub-hash.
    #
    #    User.joins(:posts).where("posts.published" => true)
    #    User.joins(:posts).where(posts: { published: true })
    #
    # === No Argument
    #
    # If no argument is passed, #where returns a new instance of WhereChain, that
    # can be chained with WhereChain#not, WhereChain#missing, or WhereChain#associated.
    #
    # Chaining with WhereChain#not:
    #
    #    User.where.not(name: "Jon")
    #    # SELECT * FROM users WHERE name != 'Jon'
    #
    # Chaining with WhereChain#associated:
    #
    #    Post.where.associated(:author)
    #    # SELECT "posts".* FROM "posts"
    #    # INNER JOIN "authors" ON "authors"."id" = "posts"."author_id"
    #    # WHERE "authors"."id" IS NOT NULL
    #
    # Chaining with WhereChain#missing:
    #
    #    Post.where.missing(:author)
    #    # SELECT "posts".* FROM "posts"
    #    # LEFT OUTER JOIN "authors" ON "authors"."id" = "posts"."author_id"
    #    # WHERE "authors"."id" IS NULL
    #
    # === Blank Condition
    #
    # If the condition is any blank-ish object, then #where is a no-op and returns
    # the current relation.
    def where(*args)
      if args.empty?
        WhereChain.new(spawn)
      elsif args.length == 1 && args.first.blank?
        self
      else
        spawn.where!(*args)
      end
    end

    def where!(opts, *rest) # :nodoc:
      self.where_clause += build_where_clause(opts, rest)
      self
    end

    # Allows you to change a previously set where condition for a given attribute, instead of appending to that condition.
    #
    #   Post.where(trashed: true).where(trashed: false)
    #   # WHERE `trashed` = 1 AND `trashed` = 0
    #
    #   Post.where(trashed: true).rewhere(trashed: false)
    #   # WHERE `trashed` = 0
    #
    #   Post.where(active: true).where(trashed: true).rewhere(trashed: false)
    #   # WHERE `active` = 1 AND `trashed` = 0
    #
    # This is short-hand for <tt>unscope(where: conditions.keys).where(conditions)</tt>.
    # Note that unlike reorder, we're only unscoping the named conditions -- not the entire where statement.
    def rewhere(conditions)
      return unscope(:where) if conditions.nil?

      scope = spawn
      where_clause = scope.build_where_clause(conditions)

      scope.unscope!(where: where_clause.extract_attributes)
      scope.where_clause += where_clause
      scope
    end

    # Allows you to invert an entire where clause instead of manually applying conditions.
    #
    #   class User
    #     scope :active, -> { where(accepted: true, locked: false) }
    #   end
    #
    #   User.where(accepted: true)
    #   # WHERE `accepted` = 1
    #
    #   User.where(accepted: true).invert_where
    #   # WHERE `accepted` != 1
    #
    #   User.active
    #   # WHERE `accepted` = 1 AND `locked` = 0
    #
    #   User.active.invert_where
    #   # WHERE NOT (`accepted` = 1 AND `locked` = 0)
    #
    # Be careful because this inverts all conditions before +invert_where+ call.
    #
    #   class User
    #     scope :active, -> { where(accepted: true, locked: false) }
    #     scope :inactive, -> { active.invert_where } # Do not attempt it
    #   end
    #
    #   # It also inverts `where(role: 'admin')` unexpectedly.
    #   User.where(role: 'admin').inactive
    #   # WHERE NOT (`role` = 'admin' AND `accepted` = 1 AND `locked` = 0)
    #
    def invert_where
      spawn.invert_where!
    end

    def invert_where! # :nodoc:
      self.where_clause = where_clause.invert
      self
    end

    # Checks whether the given relation is structurally compatible with this relation, to determine
    # if it's possible to use the #and and #or methods without raising an error. Structurally
    # compatible is defined as: they must be scoping the same model, and they must differ only by
    # #where (if no #group has been defined) or #having (if a #group is present).
    #
    #    Post.where("id = 1").structurally_compatible?(Post.where("author_id = 3"))
    #    # => true
    #
    #    Post.joins(:comments).structurally_compatible?(Post.where("id = 1"))
    #    # => false
    #
    def structurally_compatible?(other)
      structurally_incompatible_values_for(other).empty?
    end

    # Returns a new relation, which is the logical intersection of this relation and the one passed
    # as an argument.
    #
    # The two relations must be structurally compatible: they must be scoping the same model, and
    # they must differ only by #where (if no #group has been defined) or #having (if a #group is
    # present).
    #
    #    Post.where(id: [1, 2]).and(Post.where(id: [2, 3]))
    #    # SELECT `posts`.* FROM `posts` WHERE `posts`.`id` IN (1, 2) AND `posts`.`id` IN (2, 3)
    #
    def and(other)
      if other.is_a?(Relation)
        spawn.and!(other)
      else
        raise ArgumentError, "You have passed #{other.class.name} object to #and. Pass an ActiveRecord::Relation object instead."
      end
    end

    def and!(other) # :nodoc:
      incompatible_values = structurally_incompatible_values_for(other)

      unless incompatible_values.empty?
        raise ArgumentError, "Relation passed to #and must be structurally compatible. Incompatible values: #{incompatible_values}"
      end

      self.where_clause |= other.where_clause
      self.having_clause |= other.having_clause
      self.references_values |= other.references_values

      self
    end

    # Returns a new relation, which is the logical union of this relation and the one passed as an
    # argument.
    #
    # The two relations must be structurally compatible: they must be scoping the same model, and
    # they must differ only by #where (if no #group has been defined) or #having (if a #group is
    # present).
    #
    #    Post.where("id = 1").or(Post.where("author_id = 3"))
    #    # SELECT `posts`.* FROM `posts` WHERE ((id = 1) OR (author_id = 3))
    #
    def or(other)
      if other.is_a?(Relation)
        if @none
          other.spawn
        else
          spawn.or!(other)
        end
      else
        raise ArgumentError, "You have passed #{other.class.name} object to #or. Pass an ActiveRecord::Relation object instead."
      end
    end

    def or!(other) # :nodoc:
      incompatible_values = structurally_incompatible_values_for(other)

      unless incompatible_values.empty?
        raise ArgumentError, "Relation passed to #or must be structurally compatible. Incompatible values: #{incompatible_values}"
      end

      self.where_clause = self.where_clause.or(other.where_clause)
      self.having_clause = having_clause.or(other.having_clause)
      self.references_values |= other.references_values

      self
    end

    # Allows to specify a HAVING clause. Note that you can't use HAVING
    # without also specifying a GROUP clause.
    #
    #   Order.having('SUM(price) > 30').group('user_id')
    def having(opts, *rest)
      opts.blank? ? self : spawn.having!(opts, *rest)
    end

    def having!(opts, *rest) # :nodoc:
      self.having_clause += build_having_clause(opts, rest)
      self
    end

    # Specifies a limit for the number of records to retrieve.
    #
    #   User.limit(10) # generated SQL has 'LIMIT 10'
    #
    #   User.limit(10).limit(20) # generated SQL has 'LIMIT 20'
    def limit(value)
      spawn.limit!(value)
    end

    def limit!(value) # :nodoc:
      self.limit_value = value
      self
    end

    # Specifies the number of rows to skip before returning rows.
    #
    #   User.offset(10) # generated SQL has "OFFSET 10"
    #
    # Should be used with order.
    #
    #   User.offset(10).order("name ASC")
    def offset(value)
      spawn.offset!(value)
    end

    def offset!(value) # :nodoc:
      self.offset_value = value
      self
    end

    # Specifies locking settings (default to +true+). For more information
    # on locking, please see ActiveRecord::Locking.
    def lock(locks = true)
      spawn.lock!(locks)
    end

    def lock!(locks = true) # :nodoc:
      case locks
      when String, TrueClass, NilClass
        self.lock_value = locks || true
      else
        self.lock_value = false
      end

      self
    end

    # Returns a chainable relation with zero records.
    #
    # The returned relation implements the Null Object pattern. It is an
    # object with defined null behavior and always returns an empty array of
    # records without querying the database.
    #
    # Any subsequent condition chained to the returned relation will continue
    # generating an empty relation and will not fire any query to the database.
    #
    # Used in cases where a method or scope could return zero records but the
    # result needs to be chainable.
    #
    # For example:
    #
    #   @posts = current_user.visible_posts.where(name: params[:name])
    #   # the visible_posts method is expected to return a chainable Relation
    #
    #   def visible_posts
    #     case role
    #     when 'Country Manager'
    #       Post.where(country: country)
    #     when 'Reviewer'
    #       Post.published
    #     when 'Bad User'
    #       Post.none # It can't be chained if [] is returned.
    #     end
    #   end
    #
    def none
      spawn.none!
    end

    def none! # :nodoc:
      unless @none
        where!("1=0")
        @none = true
      end
      self
    end

    def null_relation? # :nodoc:
      @none
    end

    # Mark a relation as readonly. Attempting to update a record will result in
    # an error.
    #
    #   users = User.readonly
    #   users.first.save
    #   => ActiveRecord::ReadOnlyRecord: User is marked as readonly
    #
    # To make a readonly relation writable, pass +false+.
    #
    #   users.readonly(false)
    #   users.first.save
    #   => true
    def readonly(value = true)
      spawn.readonly!(value)
    end

    def readonly!(value = true) # :nodoc:
      self.readonly_value = value
      self
    end

    # Sets the returned relation to strict_loading mode. This will raise an error
    # if the record tries to lazily load an association.
    #
    #   user = User.strict_loading.first
    #   user.comments.to_a
    #   => ActiveRecord::StrictLoadingViolationError
    def strict_loading(value = true)
      spawn.strict_loading!(value)
    end

    def strict_loading!(value = true) # :nodoc:
      self.strict_loading_value = value
      self
    end

    # Sets attributes to be used when creating new records from a
    # relation object.
    #
    #   users = User.where(name: 'Oscar')
    #   users.new.name # => 'Oscar'
    #
    #   users = users.create_with(name: 'DHH')
    #   users.new.name # => 'DHH'
    #
    # You can pass +nil+ to #create_with to reset attributes:
    #
    #   users = users.create_with(nil)
    #   users.new.name # => 'Oscar'
    def create_with(value)
      spawn.create_with!(value)
    end

    def create_with!(value) # :nodoc:
      if value
        value = sanitize_forbidden_attributes(value)
        self.create_with_value = create_with_value.merge(value)
      else
        self.create_with_value = FROZEN_EMPTY_HASH
      end

      self
    end

    # Specifies the table from which the records will be fetched. For example:
    #
    #   Topic.select('title').from('posts')
    #   # SELECT title FROM posts
    #
    # Can accept other relation objects. For example:
    #
    #   Topic.select('title').from(Topic.approved)
    #   # SELECT title FROM (SELECT * FROM topics WHERE approved = 't') subquery
    #
    # Passing a second argument (string or symbol), creates the alias for the SQL from clause. Otherwise the alias "subquery" is used:
    #
    #   Topic.select('a.title').from(Topic.approved, :a)
    #   # SELECT a.title FROM (SELECT * FROM topics WHERE approved = 't') a
    #
    # It does not add multiple arguments to the SQL from clause. The last +from+ chained is the one used:
    #
    #   Topic.select('title').from(Topic.approved).from(Topic.inactive)
    #   # SELECT title FROM (SELECT topics.* FROM topics WHERE topics.active = 'f') subquery
    #
    # For multiple arguments for the SQL from clause, you can pass a string with the exact elements in the SQL from list:
    #
    #   color = "red"
    #   Color
    #     .from("colors c, JSONB_ARRAY_ELEMENTS(colored_things) AS colorvalues(colorvalue)")
    #     .where("colorvalue->>'color' = ?", color)
    #     .select("c.*").to_a
    #   # SELECT c.*
    #   # FROM colors c, JSONB_ARRAY_ELEMENTS(colored_things) AS colorvalues(colorvalue)
    #   # WHERE (colorvalue->>'color' = 'red')
    def from(value, subquery_name = nil)
      spawn.from!(value, subquery_name)
    end

    def from!(value, subquery_name = nil) # :nodoc:
      self.from_clause = Relation::FromClause.new(value, subquery_name)
      self
    end

    # Specifies whether the records should be unique or not. For example:
    #
    #   User.select(:name)
    #   # Might return two records with the same name
    #
    #   User.select(:name).distinct
    #   # Returns 1 record per distinct name
    #
    #   User.select(:name).distinct.distinct(false)
    #   # You can also remove the uniqueness
    def distinct(value = true)
      spawn.distinct!(value)
    end

    # Like #distinct, but modifies relation in place.
    def distinct!(value = true) # :nodoc:
      self.distinct_value = value
      self
    end

    # Used to extend a scope with additional methods, either through
    # a module or through a block provided.
    #
    # The object returned is a relation, which can be further extended.
    #
    # === Using a \Module
    #
    #   module Pagination
    #     def page(number)
    #       # pagination code goes here
    #     end
    #   end
    #
    #   scope = Model.all.extending(Pagination)
    #   scope.page(params[:page])
    #
    # You can also pass a list of modules:
    #
    #   scope = Model.all.extending(Pagination, SomethingElse)
    #
    # === Using a Block
    #
    #   scope = Model.all.extending do
    #     def page(number)
    #       # pagination code goes here
    #     end
    #   end
    #   scope.page(params[:page])
    #
    # You can also use a block and a module list:
    #
    #   scope = Model.all.extending(Pagination) do
    #     def per_page(number)
    #       # pagination code goes here
    #     end
    #   end
    def extending(*modules, &block)
      if modules.any? || block
        spawn.extending!(*modules, &block)
      else
        self
      end
    end

    def extending!(*modules, &block) # :nodoc:
      modules << Module.new(&block) if block
      modules.flatten!

      self.extending_values += modules
      extend(*extending_values) if extending_values.any?

      self
    end

    # Specify optimizer hints to be used in the SELECT statement.
    #
    # Example (for MySQL):
    #
    #   Topic.optimizer_hints("MAX_EXECUTION_TIME(50000)", "NO_INDEX_MERGE(topics)")
    #   # SELECT /*+ MAX_EXECUTION_TIME(50000) NO_INDEX_MERGE(topics) */ `topics`.* FROM `topics`
    #
    # Example (for PostgreSQL with pg_hint_plan):
    #
    #   Topic.optimizer_hints("SeqScan(topics)", "Parallel(topics 8)")
    #   # SELECT /*+ SeqScan(topics) Parallel(topics 8) */ "topics".* FROM "topics"
    def optimizer_hints(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.optimizer_hints!(*args)
    end

    def optimizer_hints!(*args) # :nodoc:
      self.optimizer_hints_values |= args
      self
    end

    # Reverse the existing order clause on the relation.
    #
    #   User.order('name ASC').reverse_order # generated SQL has 'ORDER BY name DESC'
    def reverse_order
      spawn.reverse_order!
    end

    def reverse_order! # :nodoc:
      orders = order_values.compact_blank
      self.order_values = reverse_sql_order(orders)
      self
    end

    def skip_query_cache!(value = true) # :nodoc:
      self.skip_query_cache_value = value
      self
    end

    def skip_preloading! # :nodoc:
      self.skip_preloading_value = true
      self
    end

    # Adds an SQL comment to queries generated from this relation. For example:
    #
    #   User.annotate("selecting user names").select(:name)
    #   # SELECT "users"."name" FROM "users" /* selecting user names */
    #
    #   User.annotate("selecting", "user", "names").select(:name)
    #   # SELECT "users"."name" FROM "users" /* selecting */ /* user */ /* names */
    #
    # The SQL block comment delimiters, "/*" and "*/", will be added automatically.
    #
    # Some escaping is performed, however untrusted user input should not be used.
    def annotate(*args)
      check_if_method_has_arguments!(__callee__, args)
      spawn.annotate!(*args)
    end

    # Like #annotate, but modifies relation in place.
    def annotate!(*args) # :nodoc:
      self.annotate_values += args
      self
    end

    # Deduplicate multiple values.
    def uniq!(name)
      if values = @values[name]
        values.uniq! if values.is_a?(Array) && !values.empty?
      end
      self
    end

    # Excludes the specified record (or collection of records) from the resulting
    # relation. For example:
    #
    #   Post.excluding(post)
    #   # SELECT "posts".* FROM "posts" WHERE "posts"."id" != 1
    #
    #   Post.excluding(post_one, post_two)
    #   # SELECT "posts".* FROM "posts" WHERE "posts"."id" NOT IN (1, 2)
    #
    # This can also be called on associations. As with the above example, either
    # a single record of collection thereof may be specified:
    #
    #   post = Post.find(1)
    #   comment = Comment.find(2)
    #   post.comments.excluding(comment)
    #   # SELECT "comments".* FROM "comments" WHERE "comments"."post_id" = 1 AND "comments"."id" != 2
    #
    # This is short-hand for <tt>.where.not(id: post.id)</tt> and <tt>.where.not(id: [post_one.id, post_two.id])</tt>.
    #
    # An <tt>ArgumentError</tt> will be raised if either no records are
    # specified, or if any of the records in the collection (if a collection
    # is passed in) are not instances of the same model that the relation is
    # scoping.
    def excluding(*records)
      records.flatten!(1)
      records.compact!

      unless records.all?(klass)
        raise ArgumentError, "You must only pass a single or collection of #{klass.name} objects to ##{__callee__}."
      end

      spawn.excluding!(records)
    end
    alias :without :excluding

    def excluding!(records) # :nodoc:
      predicates = [ predicate_builder[primary_key, records].invert ]
      self.where_clause += Relation::WhereClause.new(predicates)
      self
    end

    # Returns the Arel object associated with the relation.
    def arel(aliases = nil) # :nodoc:
      @arel ||= build_arel(aliases)
    end

    def construct_join_dependency(associations, join_type) # :nodoc:
      ActiveRecord::Associations::JoinDependency.new(
        klass, table, associations, join_type
      )
    end

    protected
      def build_subquery(subquery_alias, select_value) # :nodoc:
        subquery = except(:optimizer_hints).arel.as(subquery_alias)

        Arel::SelectManager.new(subquery).project(select_value).tap do |arel|
          arel.optimizer_hints(*optimizer_hints_values) unless optimizer_hints_values.empty?
        end
      end

      def build_where_clause(opts, rest = []) # :nodoc:
        opts = sanitize_forbidden_attributes(opts)

        case opts
        when String, Array
          parts = [klass.sanitize_sql(rest.empty? ? opts : [opts, *rest])]
        when Hash
          opts = opts.transform_keys do |key|
            if key.is_a?(Array)
              key.map { |k| klass.attribute_aliases[k.to_s] || k.to_s }
            else
              key = key.to_s
              klass.attribute_aliases[key] || key
            end
          end
          references = PredicateBuilder.references(opts)
          self.references_values |= references unless references.empty?

          parts = predicate_builder.build_from_hash(opts) do |table_name|
            lookup_table_klass_from_join_dependencies(table_name)
          end
        when Arel::Nodes::Node
          parts = [opts]
        else
          raise ArgumentError, "Unsupported argument type: #{opts} (#{opts.class})"
        end

        Relation::WhereClause.new(parts)
      end
      alias :build_having_clause :build_where_clause

      def async!
        @async = true
        self
      end

    private
      def async
        spawn.async!
      end

      def lookup_table_klass_from_join_dependencies(table_name)
        each_join_dependencies do |join|
          return join.base_klass if table_name == join.table_name
        end
        nil
      end

      def each_join_dependencies(join_dependencies = build_join_dependencies, &block)
        join_dependencies.each do |join_dependency|
          join_dependency.each(&block)
        end
      end

      def build_join_dependencies
        joins = joins_values | left_outer_joins_values
        joins |= eager_load_values unless eager_load_values.empty?
        joins |= includes_values unless includes_values.empty?

        join_dependencies = []
        join_dependencies.unshift construct_join_dependency(
          select_named_joins(joins, join_dependencies), nil
        )
      end

      def assert_mutability!
        raise ImmutableRelation if @loaded
        raise ImmutableRelation if defined?(@arel) && @arel
      end

      def build_arel(aliases = nil)
        arel = Arel::SelectManager.new(table)

        build_joins(arel.join_sources, aliases)

        arel.where(where_clause.ast) unless where_clause.empty?
        arel.having(having_clause.ast) unless having_clause.empty?
        arel.take(build_cast_value("LIMIT", connection.sanitize_limit(limit_value))) if limit_value
        arel.skip(build_cast_value("OFFSET", offset_value.to_i)) if offset_value
        arel.group(*arel_columns(group_values.uniq)) unless group_values.empty?

        build_order(arel)
        build_with(arel)
        build_select(arel)

        arel.optimizer_hints(*optimizer_hints_values) unless optimizer_hints_values.empty?
        arel.distinct(distinct_value)
        arel.from(build_from) unless from_clause.empty?
        arel.lock(lock_value) if lock_value

        unless annotate_values.empty?
          annotates = annotate_values
          annotates = annotates.uniq if annotates.size > 1
          arel.comment(*annotates)
        end

        arel
      end

      def build_cast_value(name, value)
        ActiveModel::Attribute.with_cast_value(name, value, Type.default_value)
      end

      def build_from
        opts = from_clause.value
        name = from_clause.name
        case opts
        when Relation
          if opts.eager_loading?
            opts = opts.send(:apply_join_dependency)
          end
          name ||= "subquery"
          opts.arel.as(name.to_s)
        else
          opts
        end
      end

      def select_named_joins(join_names, stashed_joins = nil, &block)
        cte_joins, associations = join_names.partition do |join_name|
          Symbol === join_name && with_values.any? { _1.key?(join_name) }
        end

        cte_joins.each do |cte_name|
          block&.call(CTEJoin.new(cte_name))
        end

        select_association_list(associations, stashed_joins, &block)
      end

      def select_association_list(associations, stashed_joins = nil)
        result = []
        associations.each do |association|
          case association
          when Hash, Symbol, Array
            result << association
          when ActiveRecord::Associations::JoinDependency
            stashed_joins&.<< association
          else
            yield association if block_given?
          end
        end
        result
      end

      def build_join_buckets
        buckets = Hash.new { |h, k| h[k] = [] }

        unless left_outer_joins_values.empty?
          stashed_left_joins = []
          left_joins = select_named_joins(left_outer_joins_values, stashed_left_joins) do |left_join|
            if left_join.is_a?(CTEJoin)
              buckets[:join_node] << build_with_join_node(left_join.name, Arel::Nodes::OuterJoin)
            else
              raise ArgumentError, "only Hash, Symbol and Array are allowed"
            end
          end

          if joins_values.empty?
            buckets[:named_join] = left_joins
            buckets[:stashed_join] = stashed_left_joins
            return buckets, Arel::Nodes::OuterJoin
          else
            stashed_left_joins.unshift construct_join_dependency(left_joins, Arel::Nodes::OuterJoin)
          end
        end

        joins = joins_values.dup
        if joins.last.is_a?(ActiveRecord::Associations::JoinDependency)
          stashed_eager_load = joins.pop if joins.last.base_klass == klass
        end

        joins.each_with_index do |join, i|
          joins[i] = Arel::Nodes::StringJoin.new(Arel.sql(join.strip)) if join.is_a?(String)
        end

        while joins.first.is_a?(Arel::Nodes::Join)
          join_node = joins.shift
          if !join_node.is_a?(Arel::Nodes::LeadingJoin) && (stashed_eager_load || stashed_left_joins)
            buckets[:join_node] << join_node
          else
            buckets[:leading_join] << join_node
          end
        end

        buckets[:named_join] = select_named_joins(joins, buckets[:stashed_join]) do |join|
          if join.is_a?(Arel::Nodes::Join)
            buckets[:join_node] << join
          elsif join.is_a?(CTEJoin)
            buckets[:join_node] << build_with_join_node(join.name)
          else
            raise "unknown class: %s" % join.class.name
          end
        end

        buckets[:stashed_join].concat stashed_left_joins if stashed_left_joins
        buckets[:stashed_join] << stashed_eager_load if stashed_eager_load

        return buckets, Arel::Nodes::InnerJoin
      end

      def build_joins(join_sources, aliases = nil)
        return join_sources if joins_values.empty? && left_outer_joins_values.empty?

        buckets, join_type = build_join_buckets

        named_joins   = buckets[:named_join]
        stashed_joins = buckets[:stashed_join]
        leading_joins = buckets[:leading_join]
        join_nodes    = buckets[:join_node]

        join_sources.concat(leading_joins) unless leading_joins.empty?

        unless named_joins.empty? && stashed_joins.empty?
          alias_tracker = alias_tracker(leading_joins + join_nodes, aliases)
          join_dependency = construct_join_dependency(named_joins, join_type)
          join_sources.concat(join_dependency.join_constraints(stashed_joins, alias_tracker, references_values))
        end

        join_sources.concat(join_nodes) unless join_nodes.empty?
        join_sources
      end

      def build_select(arel)
        if select_values.any?
          arel.project(*arel_columns(select_values))
        elsif klass.ignored_columns.any? || klass.enumerate_columns_in_select_statements
          arel.project(*klass.column_names.map { |field| table[field] })
        else
          arel.project(table[Arel.star])
        end
      end

      def build_with(arel)
        return if with_values.empty?

        with_statements = with_values.map do |with_value|
          raise ArgumentError, "Unsupported argument type: #{with_value} #{with_value.class}" unless with_value.is_a?(Hash)

          build_with_value_from_hash(with_value)
        end

        arel.with(with_statements)
      end

      def build_with_value_from_hash(hash)
        hash.map do |name, value|
          expression =
            case value
            when Arel::Nodes::SqlLiteral then Arel::Nodes::Grouping.new(value)
            when ActiveRecord::Relation then value.arel
            when Arel::SelectManager then value
            else
              raise ArgumentError, "Unsupported argument type: `#{value}` #{value.class}"
            end
          Arel::Nodes::TableAlias.new(expression, name)
        end
      end

      def build_with_join_node(name, kind = Arel::Nodes::InnerJoin)
        with_table = Arel::Table.new(name)

        table.join(with_table, kind).on(
          with_table[klass.model_name.to_s.foreign_key].eq(table[klass.primary_key])
        ).join_sources.first
      end

      def arel_columns(columns)
        columns.flat_map do |field|
          case field
          when Symbol
            arel_column(field.to_s) do |attr_name|
              connection.quote_table_name(attr_name)
            end
          when String
            arel_column(field, &:itself)
          when Proc
            field.call
          else
            field
          end
        end
      end

      def arel_column(field)
        field = klass.attribute_aliases[field] || field
        from = from_clause.name || from_clause.value

        if klass.columns_hash.key?(field) && (!from || table_name_matches?(from))
          table[field]
        elsif field.match?(/\A\w+\.\w+\z/)
          table, column = field.split(".")
          predicate_builder.resolve_arel_attribute(table, column) do
            lookup_table_klass_from_join_dependencies(table)
          end
        else
          yield field
        end
      end

      def table_name_matches?(from)
        table_name = Regexp.escape(table.name)
        quoted_table_name = Regexp.escape(connection.quote_table_name(table.name))
        /(?:\A|(?<!FROM)\s)(?:\b#{table_name}\b|#{quoted_table_name})(?!\.)/i.match?(from.to_s)
      end

      def reverse_sql_order(order_query)
        if order_query.empty?
          return [table[primary_key].desc] if primary_key
          raise IrreversibleOrderError,
            "Relation has no current order and table has no primary key to be used as default order"
        end

        order_query.flat_map do |o|
          case o
          when Arel::Attribute
            o.desc
          when Arel::Nodes::Ordering
            o.reverse
          when Arel::Nodes::NodeExpression
            o.desc
          when String
            if does_not_support_reverse?(o)
              raise IrreversibleOrderError, "Order #{o.inspect} cannot be reversed automatically"
            end
            o.split(",").map! do |s|
              s.strip!
              s.gsub!(/\sasc\Z/i, " DESC") || s.gsub!(/\sdesc\Z/i, " ASC") || (s << " DESC")
            end
          else
            o
          end
        end
      end

      def does_not_support_reverse?(order)
        # Account for String subclasses like Arel::Nodes::SqlLiteral that
        # override methods like #count.
        order = String.new(order) unless order.instance_of?(String)

        # Uses SQL function with multiple arguments.
        (order.include?(",") && order.split(",").find { |section| section.count("(") != section.count(")") }) ||
          # Uses "nulls first" like construction.
          /\bnulls\s+(?:first|last)\b/i.match?(order)
      end

      def build_order(arel)
        orders = order_values.compact_blank
        arel.order(*orders) unless orders.empty?
      end

      VALID_DIRECTIONS = [:asc, :desc, :ASC, :DESC,
                          "asc", "desc", "ASC", "DESC"].to_set # :nodoc:

      def validate_order_args(args)
        args.each do |arg|
          next unless arg.is_a?(Hash)
          arg.each do |_key, value|
            unless VALID_DIRECTIONS.include?(value)
              raise ArgumentError,
                "Direction \"#{value}\" is invalid. Valid directions are: #{VALID_DIRECTIONS.to_a.inspect}"
            end
          end
        end
      end

      def preprocess_order_args(order_args)
        @klass.disallow_raw_sql!(
          order_args.flat_map { |a| a.is_a?(Hash) ? a.keys : a },
          permit: connection.column_name_with_order_matcher
        )

        validate_order_args(order_args)

        references = column_references(order_args)
        self.references_values |= references unless references.empty?

        # if a symbol is given we prepend the quoted table name
        order_args.map! do |arg|
          case arg
          when Symbol
            order_column(arg.to_s).asc
          when Hash
            arg.map { |field, dir|
              case field
              when Arel::Nodes::SqlLiteral, Arel::Nodes::Node, Arel::Attribute
                field.public_send(dir.downcase)
              else
                order_column(field.to_s).public_send(dir.downcase)
              end
            }
          else
            arg
          end
        end.flatten!
      end

      def sanitize_order_arguments(order_args)
        order_args.map! do |arg|
          klass.sanitize_sql_for_order(arg)
        end
      end

      def column_references(order_args)
        references = order_args.flat_map do |arg|
          case arg
          when String, Symbol
            arg
          when Hash
            arg.keys.map do |key|
              key if key.is_a?(String) || key.is_a?(Symbol)
            end
          end
        end
        references.map! { |arg| arg =~ /^\W?(\w+)\W?\./ && $1 }.compact!
        references
      end

      def order_column(field)
        arel_column(field) do |attr_name|
          if attr_name == "count" && !group_values.empty?
            table[attr_name]
          else
            Arel.sql(connection.quote_table_name(attr_name))
          end
        end
      end

      def build_case_for_value_position(column, values)
        node = Arel::Nodes::Case.new
        values.each.with_index(1) do |value, order|
          node.when(column.eq(value)).then(order)
        end

        Arel::Nodes::Ascending.new(node)
      end

      def resolve_arel_attributes(attrs)
        attrs.flat_map do |attr|
          case attr
          when Arel::Predications
            attr
          when Hash
            attr.flat_map do |table, columns|
              table = table.to_s
              Array(columns).map do |column|
                predicate_builder.resolve_arel_attribute(table, column)
              end
            end
          else
            attr = attr.to_s
            if attr.include?(".")
              table, column = attr.split(".", 2)
              predicate_builder.resolve_arel_attribute(table, column)
            else
              attr
            end
          end
        end
      end

      # Checks to make sure that the arguments are not blank. Note that if some
      # blank-like object were initially passed into the query method, then this
      # method will not raise an error.
      #
      # Example:
      #
      #    Post.references()   # raises an error
      #    Post.references([]) # does not raise an error
      #
      # This particular method should be called with a method_name (__callee__) and the args
      # passed into that method as an input. For example:
      #
      # def references(*args)
      #   check_if_method_has_arguments!(__callee__, args)
      #   ...
      # end
      def check_if_method_has_arguments!(method_name, args, message = nil)
        if args.blank?
          raise ArgumentError, message || "The method .#{method_name}() must contain arguments."
        else
          yield args if block_given?

          args.flatten!
          args.compact_blank!
        end
      end

      def process_select_args(fields)
        fields.flat_map do |field|
          if field.is_a?(Hash)
            transform_select_hash_values(field)
          else
            field
          end
        end
      end

      def transform_select_hash_values(fields)
        fields.flat_map do |key, columns_aliases|
          case columns_aliases
          when Hash
            columns_aliases.map do |column, column_alias|
              if values[:joins]&.include?(key)
                references = PredicateBuilder.references({ key.to_s => fields[key] })
                self.references_values |= references unless references.empty?
              end
              arel_column("#{key}.#{column}") do
                predicate_builder.resolve_arel_attribute(key.to_s, column)
              end.as(column_alias.to_s)
            end
          when Array
            columns_aliases.map do |column|
              arel_column("#{key}.#{column}", &:itself)
            end
          when String, Symbol
            arel_column(key.to_s) do
              predicate_builder.resolve_arel_attribute(klass.table_name, key.to_s)
            end.as(columns_aliases.to_s)
          end
        end
      end

      STRUCTURAL_VALUE_METHODS = (
        Relation::VALUE_METHODS -
        [:extending, :where, :having, :unscope, :references, :annotate, :optimizer_hints]
      ).freeze # :nodoc:

      def structurally_incompatible_values_for(other)
        values = other.values
        STRUCTURAL_VALUE_METHODS.reject do |method|
          v1, v2 = @values[method], values[method]
          if v1.is_a?(Array)
            next true unless v2.is_a?(Array)
            v1 = v1.uniq
            v2 = v2.uniq
          end
          v1 == v2
        end
      end
  end
end


// --- rb_sinatra_base.rb ---
# frozen_string_literal: true

# external dependencies
require 'rack'
begin
  require 'rackup'
rescue LoadError
end
require 'tilt'
require 'rack/protection'
require 'rack/session'
require 'mustermann'
require 'mustermann/sinatra'
require 'mustermann/regular'

# stdlib dependencies
require 'ipaddr'
require 'time'
require 'uri'

# other files we need
require 'sinatra/indifferent_hash'
require 'sinatra/show_exceptions'
require 'sinatra/version'

require_relative 'middleware/logger'

module Sinatra
  # The request object. See Rack::Request for more info:
  # https://rubydoc.info/github/rack/rack/main/Rack/Request
  class Request < Rack::Request
    HEADER_PARAM = /\s*[\w.]+=(?:[\w.]+|"(?:[^"\\]|\\.)*")?\s*/.freeze
    HEADER_VALUE_WITH_PARAMS = %r{(?:(?:\w+|\*)/(?:\w+(?:\.|-|\+)?|\*)*)\s*(?:;#{HEADER_PARAM})*}.freeze

    # Returns an array of acceptable media types for the response
    def accept
      @env['sinatra.accept'] ||= if @env.include?('HTTP_ACCEPT') && (@env['HTTP_ACCEPT'].to_s != '')
                                   @env['HTTP_ACCEPT']
                                     .to_s
                                     .scan(HEADER_VALUE_WITH_PARAMS)
                                     .map! { |e| AcceptEntry.new(e) }
                                     .sort
                                 else
                                   [AcceptEntry.new('*/*')]
                                 end
    end

    def accept?(type)
      preferred_type(type).to_s.include?(type)
    end

    def preferred_type(*types)
      return accept.first if types.empty?

      types.flatten!
      return types.first if accept.empty?

      accept.detect do |accept_header|
        type = types.detect { |t| MimeTypeEntry.new(t).accepts?(accept_header) }
        return type if type
      end
    end

    alias secure? ssl?

    def forwarded?
      !forwarded_authority.nil?
    end

    def safe?
      get? || head? || options? || trace?
    end

    def idempotent?
      safe? || put? || delete? || link? || unlink?
    end

    def link?
      request_method == 'LINK'
    end

    def unlink?
      request_method == 'UNLINK'
    end

    def params
      super
    rescue Rack::Utils::ParameterTypeError, Rack::Utils::InvalidParameterError => e
      raise BadRequest, "Invalid query parameters: #{Rack::Utils.escape_html(e.message)}"
    rescue EOFError => e
      raise BadRequest, "Invalid multipart/form-data: #{Rack::Utils.escape_html(e.message)}"
    end

    class AcceptEntry
      attr_accessor :params
      attr_reader :entry

      def initialize(entry)
        params = entry.scan(HEADER_PARAM).map! do |s|
          key, value = s.strip.split('=', 2)
          value = value[1..-2].gsub(/\\(.)/, '\1') if value.start_with?('"')
          [key, value]
        end

        @entry  = entry
        @type   = entry[/[^;]+/].delete(' ')
        @params = params.to_h
        @q      = @params.delete('q') { 1.0 }.to_f
      end

      def <=>(other)
        other.priority <=> priority
      end

      def priority
        # We sort in descending order; better matches should be higher.
        [@q, -@type.count('*'), @params.size]
      end

      def to_str
        @type
      end

      def to_s(full = false)
        full ? entry : to_str
      end

      def respond_to?(*args)
        super || to_str.respond_to?(*args)
      end

      def method_missing(*args, &block)
        to_str.send(*args, &block)
      end
    end

    class MimeTypeEntry
      attr_reader :params

      def initialize(entry)
        params = entry.scan(HEADER_PARAM).map! do |s|
          key, value = s.strip.split('=', 2)
          value = value[1..-2].gsub(/\\(.)/, '\1') if value.start_with?('"')
          [key, value]
        end

        @type   = entry[/[^;]+/].delete(' ')
        @params = params.to_h
      end

      def accepts?(entry)
        File.fnmatch(entry, self) && matches_params?(entry.params)
      end

      def to_str
        @type
      end

      def matches_params?(params)
        return true if @params.empty?

        params.all? { |k, v| !@params.key?(k) || @params[k] == v }
      end
    end
  end

  # The response object. See Rack::Response and Rack::Response::Helpers for
  # more info:
  # https://rubydoc.info/github/rack/rack/main/Rack/Response
  # https://rubydoc.info/github/rack/rack/main/Rack/Response/Helpers
  class Response < Rack::Response
    DROP_BODY_RESPONSES = [204, 304].freeze

    def body=(value)
      value = value.body while Rack::Response === value
      @body = String === value ? [value.to_str] : value
    end

    def each
      block_given? ? super : enum_for(:each)
    end

    def finish
      result = body

      if drop_content_info?
        headers.delete 'content-length'
        headers.delete 'content-type'
      end

      if drop_body?
        close
        result = []
      end

      if calculate_content_length?
        # if some other code has already set content-length, don't muck with it
        # currently, this would be the static file-handler
        headers['content-length'] = body.map(&:bytesize).reduce(0, :+).to_s
      end

      [status, headers, result]
    end

    private

    def calculate_content_length?
      headers['content-type'] && !headers['content-length'] && (Array === body)
    end

    def drop_content_info?
      informational? || drop_body?
    end

    def drop_body?
      DROP_BODY_RESPONSES.include?(status)
    end
  end

  # Some Rack handlers implement an extended body object protocol, however,
  # some middleware (namely Rack::Lint) will break it by not mirroring the methods in question.
  # This middleware will detect an extended body object and will make sure it reaches the
  # handler directly. We do this here, so our middleware and middleware set up by the app will
  # still be able to run.
  class ExtendedRack < Struct.new(:app)
    def call(env)
      result = app.call(env)
      callback = env['async.callback']
      return result unless callback && async?(*result)

      after_response { callback.call result }
      setup_close(env, *result)
      throw :async
    end

    private

    def setup_close(env, _status, _headers, body)
      return unless body.respond_to?(:close) && env.include?('async.close')

      env['async.close'].callback { body.close }
      env['async.close'].errback { body.close }
    end

    def after_response(&block)
      raise NotImplementedError, 'only supports EventMachine at the moment' unless defined? EventMachine

      EventMachine.next_tick(&block)
    end

    def async?(status, _headers, body)
      return true if status == -1

      body.respond_to?(:callback) && body.respond_to?(:errback)
    end
  end

  # Behaves exactly like Rack::CommonLogger with the notable exception that it does nothing,
  # if another CommonLogger is already in the middleware chain.
  class CommonLogger < Rack::CommonLogger
    def call(env)
      env['sinatra.commonlogger'] ? @app.call(env) : super
    end

    superclass.class_eval do
      alias_method :call_without_check, :call unless method_defined? :call_without_check
      def call(env)
        env['sinatra.commonlogger'] = true
        call_without_check(env)
      end
    end
  end

  class Error < StandardError # :nodoc:
  end

  class BadRequest < Error # :nodoc:
    def http_status; 400 end
  end

  class NotFound < Error # :nodoc:
    def http_status; 404 end
  end

  # Methods available to routes, before/after filters, and views.
  module Helpers
    # Set or retrieve the response status code.
    def status(value = nil)
      response.status = Rack::Utils.status_code(value) if value
      response.status
    end

    # Set or retrieve the response body. When a block is given,
    # evaluation is deferred until the body is read with #each.
    def body(value = nil, &block)
      if block_given?
        def block.each; yield(call) end
        response.body = block
      elsif value
        unless request.head? || value.is_a?(Rack::Files::BaseIterator) || value.is_a?(Stream)
          headers.delete 'content-length'
        end
        response.body = value
      else
        response.body
      end
    end

    # Halt processing and redirect to the URI provided.
    def redirect(uri, *args)
      http_version = env['SERVER_PROTOCOL']
      if (http_version == 'HTTP/1.1') && (env['REQUEST_METHOD'] != 'GET')
        status 303
      else
        status 302
      end

      # According to RFC 2616 section 14.30, "the field value consists of a
      # single absolute URI"
      response['Location'] = uri(uri.to_s, settings.absolute_redirects?, settings.prefixed_redirects?)
      halt(*args)
    end

    # Generates the absolute URI for a given path in the app.
    # Takes Rack routers and reverse proxies into account.
    def uri(addr = nil, absolute = true, add_script_name = true)
      return addr if addr.to_s =~ /\A[a-z][a-z0-9+.\-]*:/i

      uri = [host = String.new]
      if absolute
        host << "http#{'s' if request.secure?}://"
        host << if request.forwarded? || (request.port != (request.secure? ? 443 : 80))
                  request.host_with_port
                else
                  request.host
                end
      end
      uri << request.script_name.to_s if add_script_name
      uri << (addr || request.path_info).to_s
      File.join uri
    end

    alias url uri
    alias to uri

    # Halt processing and return the error status provided.
    def error(code, body = nil)
      if code.respond_to? :to_str
        body = code.to_str
        code = 500
      end
      response.body = body unless body.nil?
      halt code
    end

    # Halt processing and return a 404 Not Found.
    def not_found(body = nil)
      error 404, body
    end

    # Set multiple response headers with Hash.
    def headers(hash = nil)
      response.headers.merge! hash if hash
      response.headers
    end

    # Access the underlying Rack session.
    def session
      request.session
    end

    # Access shared logger object.
    def logger
      request.logger
    end

    # Look up a media type by file extension in Rack's mime registry.
    def mime_type(type)
      Base.mime_type(type)
    end

    # Set the content-type of the response body given a media type or file
    # extension.
    def content_type(type = nil, params = {})
      return response['content-type'] unless type

      default = params.delete :default
      mime_type = mime_type(type) || default
      raise format('Unknown media type: %p', type) if mime_type.nil?

      mime_type = mime_type.dup
      unless params.include?(:charset) || settings.add_charset.all? { |p| !(p === mime_type) }
        params[:charset] = params.delete('charset') || settings.default_encoding
      end
      params.delete :charset if mime_type.include? 'charset'
      unless params.empty?
        mime_type << ';'
        mime_type << params.map do |key, val|
          val = val.inspect if val.to_s =~ /[";,]/
          "#{key}=#{val}"
        end.join(';')
      end
      response['content-type'] = mime_type
    end

    # https://html.spec.whatwg.org/#multipart-form-data
    MULTIPART_FORM_DATA_REPLACEMENT_TABLE = {
      '"'  => '%22',
      "\r" => '%0D',
      "\n" => '%0A'
    }.freeze

    # Set the Content-Disposition to "attachment" with the specified filename,
    # instructing the user agents to prompt to save.
    def attachment(filename = nil, disposition = :attachment)
      response['Content-Disposition'] = disposition.to_s.dup
      return unless filename

      params = format('; filename="%s"', File.basename(filename).gsub(/["\r\n]/, MULTIPART_FORM_DATA_REPLACEMENT_TABLE))
      response['Content-Disposition'] << params
      ext = File.extname(filename)
      content_type(ext) unless response['content-type'] || ext.empty?
    end

    # Use the contents of the file at +path+ as the response body.
    def send_file(path, opts = {})
      if opts[:type] || !response['content-type']
        content_type opts[:type] || File.extname(path), default: 'application/octet-stream'
      end

      disposition = opts[:disposition]
      filename    = opts[:filename]
      disposition = :attachment if disposition.nil? && filename
      filename    = path        if filename.nil?
      attachment(filename, disposition) if disposition

      last_modified opts[:last_modified] if opts[:last_modified]

      file   = Rack::Files.new(File.dirname(settings.app_file))
      result = file.serving(request, path)

      result[1].each { |k, v| headers[k] ||= v }
      headers['content-length'] = result[1]['content-length']
      opts[:status] &&= Integer(opts[:status])
      halt (opts[:status] || result[0]), result[2]
    rescue Errno::ENOENT
      not_found
    end

    # Class of the response body in case you use #stream.
    #
    # Three things really matter: The front and back block (back being the
    # block generating content, front the one sending it to the client) and
    # the scheduler, integrating with whatever concurrency feature the Rack
    # handler is using.
    #
    # Scheduler has to respond to defer and schedule.
    class Stream
      def self.schedule(*) yield end
      def self.defer(*)    yield end

      def initialize(scheduler = self.class, keep_open = false, &back)
        @back = back.to_proc
        @scheduler = scheduler
        @keep_open = keep_open
        @callbacks = []
        @closed = false
      end

      def close
        return if closed?

        @closed = true
        @scheduler.schedule { @callbacks.each { |c| c.call } }
      end

      def each(&front)
        @front = front
        @scheduler.defer do
          begin
            @back.call(self)
          rescue Exception => e
            @scheduler.schedule { raise e }
          ensure
            close unless @keep_open
          end
        end
      end

      def <<(data)
        @scheduler.schedule { @front.call(data.to_s) }
        self
      end

      def callback(&block)
        return yield if closed?

        @callbacks << block
      end

      alias errback callback

      def closed?
        @closed
      end
    end

    # Allows to start sending data to the client even though later parts of
    # the response body have not yet been generated.
    #
    # The close parameter specifies whether Stream#close should be called
    # after the block has been executed.
    def stream(keep_open = false)
      scheduler = env['async.callback'] ? EventMachine : Stream
      current   = @params.dup
      stream = if scheduler == Stream  && keep_open
        Stream.new(scheduler, false) do |out|
          until out.closed?
            with_params(current) { yield(out) }
          end
        end
      else
        Stream.new(scheduler, keep_open) { |out| with_params(current) { yield(out) } }
      end
      body stream
    end

    # Specify response freshness policy for HTTP caches (Cache-Control header).
    # Any number of non-value directives (:public, :private, :no_cache,
    # :no_store, :must_revalidate, :proxy_revalidate) may be passed along with
    # a Hash of value directives (:max_age, :s_maxage).
    #
    #   cache_control :public, :must_revalidate, :max_age => 60
    #   => Cache-Control: public, must-revalidate, max-age=60
    #
    # See RFC 2616 / 14.9 for more on standard cache control directives:
    # http://tools.ietf.org/html/rfc2616#section-14.9.1
    def cache_control(*values)
      if values.last.is_a?(Hash)
        hash = values.pop
        hash.reject! { |_k, v| v == false }
        hash.reject! { |k, v| values << k if v == true }
      else
        hash = {}
      end

      values.map! { |value| value.to_s.tr('_', '-') }
      hash.each do |key, value|
        key = key.to_s.tr('_', '-')
        value = value.to_i if %w[max-age s-maxage].include? key
        values << "#{key}=#{value}"
      end

      response['Cache-Control'] = values.join(', ') if values.any?
    end

    # Set the Expires header and Cache-Control/max-age directive. Amount
    # can be an integer number of seconds in the future or a Time object
    # indicating when the response should be considered "stale". The remaining
    # "values" arguments are passed to the #cache_control helper:
    #
    #   expires 500, :public, :must_revalidate
    #   => Cache-Control: public, must-revalidate, max-age=500
    #   => Expires: Mon, 08 Jun 2009 08:50:17 GMT
    #
    def expires(amount, *values)
      values << {} unless values.last.is_a?(Hash)

      if amount.is_a? Integer
        time    = Time.now + amount.to_i
        max_age = amount
      else
        time    = time_for amount
        max_age = time - Time.now
      end

      values.last.merge!(max_age: max_age) { |_key, v1, v2| v1 || v2 }
      cache_control(*values)

      response['Expires'] = time.httpdate
    end

    # Set the last modified time of the resource (HTTP 'Last-Modified' header)
    # and halt if conditional GET matches. The +time+ argument is a Time,
    # DateTime, or other object that responds to +to_time+.
    #
    # When the current request includes an 'If-Modified-Since' header that is
    # equal or later than the time specified, execution is immediately halted
    # with a '304 Not Modified' response.
    def last_modified(time)
      return unless time

      time = time_for time
      response['Last-Modified'] = time.httpdate
      return if env['HTTP_IF_NONE_MATCH']

      if (status == 200) && env['HTTP_IF_MODIFIED_SINCE']
        # compare based on seconds since epoch
        since = Time.httpdate(env['HTTP_IF_MODIFIED_SINCE']).to_i
        halt 304 if since >= time.to_i
      end

      if (success? || (status == 412)) && env['HTTP_IF_UNMODIFIED_SINCE']
        # compare based on seconds since epoch
        since = Time.httpdate(env['HTTP_IF_UNMODIFIED_SINCE']).to_i
        halt 412 if since < time.to_i
      end
    rescue ArgumentError
    end

    ETAG_KINDS = %i[strong weak].freeze
    # Set the response entity tag (HTTP 'ETag' header) and halt if conditional
    # GET matches. The +value+ argument is an identifier that uniquely
    # identifies the current version of the resource. The +kind+ argument
    # indicates whether the etag should be used as a :strong (default) or :weak
    # cache validator.
    #
    # When the current request includes an 'If-None-Match' header with a
    # matching etag, execution is immediately halted. If the request method is
    # GET or HEAD, a '304 Not Modified' response is sent.
    def etag(value, options = {})
      # Before touching this code, please double check RFC 2616 14.24 and 14.26.
      options      = { kind: options } unless Hash === options
      kind         = options[:kind] || :strong
      new_resource = options.fetch(:new_resource) { request.post? }

      unless ETAG_KINDS.include?(kind)
        raise ArgumentError, ':strong or :weak expected'
      end

      value = format('"%s"', value)
      value = "W/#{value}" if kind == :weak
      response['ETag'] = value

      return unless success? || status == 304

      if etag_matches?(env['HTTP_IF_NONE_MATCH'], new_resource)
        halt(request.safe? ? 304 : 412)
      end

      if env['HTTP_IF_MATCH']
        return if etag_matches?(env['HTTP_IF_MATCH'], new_resource)

        halt 412
      end

      nil
    end

    # Sugar for redirect (example:  redirect back)
    def back
      request.referer
    end

    # whether or not the status is set to 1xx
    def informational?
      status.between? 100, 199
    end

    # whether or not the status is set to 2xx
    def success?
      status.between? 200, 299
    end

    # whether or not the status is set to 3xx
    def redirect?
      status.between? 300, 399
    end

    # whether or not the status is set to 4xx
    def client_error?
      status.between? 400, 499
    end

    # whether or not the status is set to 5xx
    def server_error?
      status.between? 500, 599
    end

    # whether or not the status is set to 404
    def not_found?
      status == 404
    end

    # whether or not the status is set to 400
    def bad_request?
      status == 400
    end

    # Generates a Time object from the given value.
    # Used by #expires and #last_modified.
    def time_for(value)
      if value.is_a? Numeric
        Time.at value
      elsif value.respond_to? :to_s
        Time.parse value.to_s
      else
        value.to_time
      end
    rescue ArgumentError => e
      raise e
    rescue Exception
      raise ArgumentError, "unable to convert #{value.inspect} to a Time object"
    end

    private

    # Helper method checking if a ETag value list includes the current ETag.
    def etag_matches?(list, new_resource = request.post?)
      return !new_resource if list == '*'

      list.to_s.split(',').map(&:strip).include?(response['ETag'])
    end

    def with_params(temp_params)
      original = @params
      @params = temp_params
      yield
    ensure
      @params = original if original
    end
  end

  # Template rendering methods. Each method takes the name of a template
  # to render as a Symbol and returns a String with the rendered output,
  # as well as an optional hash with additional options.
  #
  # `template` is either the name or path of the template as symbol
  # (Use `:'subdir/myview'` for views in subdirectories), or a string
  # that will be rendered.
  #
  # Possible options are:
  #   :content_type   The content type to use, same arguments as content_type.
  #   :layout         If set to something falsy, no layout is rendered, otherwise
  #                   the specified layout is used (Ignored for `sass`)
  #   :layout_engine  Engine to use for rendering the layout.
  #   :locals         A hash with local variables that should be available
  #                   in the template
  #   :scope          If set, template is evaluate with the binding of the given
  #                   object rather than the application instance.
  #   :views          Views directory to use.
  module Templates
    module ContentTyped
      attr_accessor :content_type
    end

    def initialize
      super
      @default_layout = :layout
      @preferred_extension = nil
    end

    def erb(template, options = {}, locals = {}, &block)
      render(:erb, template, options, locals, &block)
    end

    def haml(template, options = {}, locals = {}, &block)
      render(:haml, template, options, locals, &block)
    end

    def sass(template, options = {}, locals = {})
      options[:default_content_type] = :css
      options[:exclude_outvar] = true
      options[:layout] = nil
      render :sass, template, options, locals
    end

    def scss(template, options = {}, locals = {})
      options[:default_content_type] = :css
      options[:exclude_outvar] = true
      options[:layout] = nil
      render :scss, template, options, locals
    end

    def builder(template = nil, options = {}, locals = {}, &block)
      options[:default_content_type] = :xml
      render_ruby(:builder, template, options, locals, &block)
    end

    def liquid(template, options = {}, locals = {}, &block)
      render(:liquid, template, options, locals, &block)
    end

    def markdown(template, options = {}, locals = {})
      options[:exclude_outvar] = true
      render :markdown, template, options, locals
    end

    def rdoc(template, options = {}, locals = {})
      render :rdoc, template, options, locals
    end

    def asciidoc(template, options = {}, locals = {})
      render :asciidoc, template, options, locals
    end

    def markaby(template = nil, options = {}, locals = {}, &block)
      render_ruby(:mab, template, options, locals, &block)
    end

    def nokogiri(template = nil, options = {}, locals = {}, &block)
      options[:default_content_type] = :xml
      render_ruby(:nokogiri, template, options, locals, &block)
    end

    def slim(template, options = {}, locals = {}, &block)
      render(:slim, template, options, locals, &block)
    end

    def yajl(template, options = {}, locals = {})
      options[:default_content_type] = :json
      render :yajl, template, options, locals
    end

    def rabl(template, options = {}, locals = {})
      Rabl.register!
      render :rabl, template, options, locals
    end

    # Calls the given block for every possible template file in views,
    # named name.ext, where ext is registered on engine.
    def find_template(views, name, engine)
      yield ::File.join(views, "#{name}.#{@preferred_extension}")

      Tilt.default_mapping.extensions_for(engine).each do |ext|
        yield ::File.join(views, "#{name}.#{ext}") unless ext == @preferred_extension
      end
    end

    private

    # logic shared between builder and nokogiri
    def render_ruby(engine, template, options = {}, locals = {}, &block)
      if template.is_a?(Hash)
        options = template
        template = nil
      end
      template = proc { block } if template.nil?
      render engine, template, options, locals
    end

    def render(engine, data, options = {}, locals = {}, &block)
      # merge app-level options
      engine_options = settings.respond_to?(engine) ? settings.send(engine) : {}
      options.merge!(engine_options) { |_key, v1, _v2| v1 }

      # extract generic options
      locals          = options.delete(:locals) || locals         || {}
      views           = options.delete(:views)  || settings.views || './views'
      layout          = options[:layout]
      layout          = false if layout.nil? && options.include?(:layout)
      eat_errors      = layout.nil?
      layout          = engine_options[:layout] if layout.nil? || (layout == true && engine_options[:layout] != false)
      layout          = @default_layout         if layout.nil? || (layout == true)
      layout_options  = options.delete(:layout_options) || {}
      content_type    = options.delete(:default_content_type)
      content_type    = options.delete(:content_type)   || content_type
      layout_engine   = options.delete(:layout_engine)  || engine
      scope           = options.delete(:scope)          || self
      exclude_outvar  = options.delete(:exclude_outvar)
      options.delete(:layout)

      # set some defaults
      options[:outvar] ||= '@_out_buf' unless exclude_outvar
      options[:default_encoding] ||= settings.default_encoding

      # compile and render template
      begin
        layout_was      = @default_layout
        @default_layout = false
        template        = compile_template(engine, data, options, views)
        output          = template.render(scope, locals, &block)
      ensure
        @default_layout = layout_was
      end

      # render layout
      if layout
        extra_options = { views: views, layout: false, eat_errors: eat_errors, scope: scope }
        options = options.merge(extra_options).merge!(layout_options)

        catch(:layout_missing) { return render(layout_engine, layout, options, locals) { output } }
      end

      if content_type
        # sass-embedded returns a frozen string
        output = +output
        output.extend(ContentTyped).content_type = content_type
      end
      output
    end

    def compile_template(engine, data, options, views)
      eat_errors = options.delete :eat_errors
      template = Tilt[engine]
      raise "Template engine not found: #{engine}" if template.nil?

      case data
      when Symbol
        template_cache.fetch engine, data, options, views do
          body, path, line = settings.templates[data]
          if body
            body = body.call if body.respond_to?(:call)
            template.new(path, line.to_i, options) { body }
          else
            found = false
            @preferred_extension = engine.to_s
            find_template(views, data, template) do |file|
              path ||= file # keep the initial path rather than the last one
              found = File.exist?(file)
              if found
                path = file
                break
              end
            end
            throw :layout_missing if eat_errors && !found
            template.new(path, 1, options)
          end
        end
      when Proc
        compile_block_template(template, options, &data)
      when String
        template_cache.fetch engine, data, options, views do
          compile_block_template(template, options) { data }
        end
      else
        raise ArgumentError, "Sorry, don't know how to render #{data.inspect}."
      end
    end

    def compile_block_template(template, options, &body)
      first_location = caller_locations.first
      path = first_location.path
      line = first_location.lineno
      path = options[:path] || path
      line = options[:line] || line
      template.new(path, line.to_i, options, &body)
    end
  end

  # Extremely simple template cache implementation.
  #   * Not thread-safe.
  #   * Size is unbounded.
  #   * Keys are not copied defensively, and should not be modified after
  #     being passed to #fetch.  More specifically, the values returned by
  #     key#hash and key#eql? should not change.
  #
  # Implementation copied from Tilt::Cache.
  class TemplateCache
    def initialize
      @cache = {}
    end

    # Caches a value for key, or returns the previously cached value.
    # If a value has been previously cached for key then it is
    # returned. Otherwise, block is yielded to and its return value
    # which may be nil, is cached under key and returned.
    def fetch(*key)
      @cache.fetch(key) do
        @cache[key] = yield
      end
    end

    # Clears the cache.
    def clear
      @cache = {}
    end
  end

  # Base class for all Sinatra applications and middleware.
  class Base
    include Rack::Utils
    include Helpers
    include Templates

    URI_INSTANCE = defined?(URI::RFC2396_PARSER) ? URI::RFC2396_PARSER : URI::RFC2396_Parser.new

    attr_accessor :app, :env, :request, :response, :params
    attr_reader   :template_cache

    def initialize(app = nil, **_kwargs)
      super()
      @app = app
      @template_cache = TemplateCache.new
      @pinned_response = nil # whether a before! filter pinned the content-type
      yield self if block_given?
    end

    # Rack call interface.
    def call(env)
      dup.call!(env)
    end

    def call!(env) # :nodoc:
      @env      = env
      @params   = IndifferentHash.new
      @request  = Request.new(env)
      @response = Response.new
      @pinned_response = nil
      template_cache.clear if settings.reload_templates

      invoke { dispatch! }
      invoke { error_block!(response.status) } unless @env['sinatra.error']

      unless @response['content-type']
        if Array === body && body[0].respond_to?(:content_type)
          content_type body[0].content_type
        elsif (default = settings.default_content_type)
          content_type default
        end
      end

      @response.finish
    end

    # Access settings defined with Base.set.
    def self.settings
      self
    end

    # Access settings defined with Base.set.
    def settings
      self.class.settings
    end

    # Exit the current block, halts any further processing
    # of the request, and returns the specified response.
    def halt(*response)
      response = response.first if response.length == 1
      throw :halt, response
    end

    # Pass control to the next matching route.
    # If there are no more matching routes, Sinatra will
    # return a 404 response.
    def pass(&block)
      throw :pass, block
    end

    # Forward the request to the downstream app -- middleware only.
    def forward
      raise 'downstream app not set' unless @app.respond_to? :call

      status, headers, body = @app.call env
      @response.status = status
      @response.body = body
      @response.headers.merge! headers
      nil
    end

    private

    # Run filters defined on the class and all superclasses.
    # Accepts an optional block to call after each filter is applied.
    def filter!(type, base = settings, &block)
      filter!(type, base.superclass, &block) if base.superclass.respond_to?(:filters)
      base.filters[type].each do |args|
        result = process_route(*args)
        block.call(result) if block_given?
      end
    end

    # Run routes defined on the class and all superclasses.
    def route!(base = settings, pass_block = nil)
      routes = base.routes[@request.request_method]

      routes&.each do |pattern, conditions, block|
        response.delete_header('content-type') unless @pinned_response

        returned_pass_block = process_route(pattern, conditions) do |*args|
          env['sinatra.route'] = "#{@request.request_method} #{pattern}"
          route_eval { block[*args] }
        end

        # don't wipe out pass_block in superclass
        pass_block = returned_pass_block if returned_pass_block
      end

      # Run routes defined in superclass.
      if base.superclass.respond_to?(:routes)
        return route!(base.superclass, pass_block)
      end

      route_eval(&pass_block) if pass_block
      route_missing
    end

    # Run a route block and throw :halt with the result.
    def route_eval
      throw :halt, yield
    end

    # If the current request matches pattern and conditions, fill params
    # with keys and call the given block.
    # Revert params afterwards.
    #
    # Returns pass block.
    def process_route(pattern, conditions, block = nil, values = [])
      route = @request.path_info
      route = '/' if route.empty? && !settings.empty_path_info?
      route = route[0..-2] if !settings.strict_paths? && route != '/' && route.end_with?('/')

      params = pattern.params(route)
      return unless params

      params.delete('ignore') # TODO: better params handling, maybe turn it into "smart" object or detect changes
      force_encoding(params)
      @params = @params.merge(params) { |_k, v1, v2| v2 || v1 } if params.any?

      regexp_exists = pattern.is_a?(Mustermann::Regular) || (pattern.respond_to?(:patterns) && pattern.patterns.any? { |subpattern| subpattern.is_a?(Mustermann::Regular) })
      if regexp_exists
        captures           = pattern.match(route).captures.map { |c| URI_INSTANCE.unescape(c) if c }
        values            += captures
        @params[:captures] = force_encoding(captures) unless captures.nil? || captures.empty?
      else
        values += params.values.flatten
      end

      catch(:pass) do
        conditions.each { |c| throw :pass if c.bind(self).call == false }
        block ? block[self, values] : yield(self, values)
      end
    rescue StandardError
      @env['sinatra.error.params'] = @params
      raise
    ensure
      params ||= {}
      params.each { |k, _| @params.delete(k) } unless @env['sinatra.error.params']
    end

    # No matching route was found or all routes passed. The default
    # implementation is to forward the request downstream when running
    # as middleware (@app is non-nil); when no downstream app is set, raise
    # a NotFound exception. Subclasses can override this method to perform
    # custom route miss logic.
    def route_missing
      raise NotFound unless @app

      forward
    end

    # Attempt to serve static files from public directory. Throws :halt when
    # a matching file is found, returns nil otherwise.
    # If custom static headers are defined, use them.
    def static!(options = {})
      return if (public_dir = settings.public_folder).nil?

      path = "#{public_dir}#{URI_INSTANCE.unescape(request.path_info)}"
      return unless valid_path?(path)

      path = File.expand_path(path)
      return unless path.start_with?("#{File.expand_path(public_dir)}/")

      return unless File.file?(path)

      env['sinatra.static_file'] = path
      cache_control(*settings.static_cache_control) if settings.static_cache_control?

      headers(settings.static_headers) if settings.static_headers?

      send_file path, options.merge(disposition: nil)
    end

    # Run the block with 'throw :halt' support and apply result to the response.
    def invoke(&block)
      res = catch(:halt, &block)

      res = [res] if (Integer === res) || (String === res)
      if (Array === res) && (Integer === res.first)
        res = res.dup
        status(res.shift)
        body(res.pop)
        headers(*res)
      elsif res.respond_to? :each
        body res
      end
      nil # avoid double setting the same response tuple twice
    end

    # Dispatch a request with error handling.
    def dispatch!
      # Avoid passing frozen string in force_encoding
      @params.merge!(@request.params).each do |key, val|
        next unless val.respond_to?(:force_encoding)

        val = val.dup if val.frozen?
        @params[key] = force_encoding(val)
      end

      invoke do
        static! if settings.static? && (request.get? || request.head?)
        filter! :before do
          @pinned_response = !response['content-type'].nil?
        end
        route!
      end
    rescue ::Exception => e
      invoke { handle_exception!(e) }
    ensure
      begin
        filter! :after unless env['sinatra.static_file']
      rescue ::Exception => e
        invoke { handle_exception!(e) } unless @env['sinatra.error']
      end
    end

    # Error handling during requests.
    def handle_exception!(boom)
      error_params = @env['sinatra.error.params']

      @params = @params.merge(error_params) if error_params

      @env['sinatra.error'] = boom

      http_status = if boom.is_a? Sinatra::Error
                      if boom.respond_to? :http_status
                        boom.http_status
                      elsif settings.use_code? && boom.respond_to?(:code)
                        boom.code
                      end
                    end

      http_status = 500 unless http_status&.between?(400, 599)
      status(http_status)

      if server_error?
        dump_errors! boom if settings.dump_errors?
        raise boom if settings.show_exceptions? && (settings.show_exceptions != :after_handler)
      elsif not_found?
        headers['X-Cascade'] = 'pass' if settings.x_cascade?
      end

      if (res = error_block!(boom.class, boom) || error_block!(status, boom))
        return res
      end

      if not_found? || bad_request?
        if boom.message && boom.message != boom.class.name
          body Rack::Utils.escape_html(boom.message)
        else
          content_type 'text/html'
          body "<h1>#{not_found? ? 'Not Found' : 'Bad Request'}</h1>"
        end
      end

      return unless server_error?

      raise boom if settings.raise_errors? || settings.show_exceptions?

      error_block! Exception, boom
    end

    # Find an custom error block for the key(s) specified.
    def error_block!(key, *block_params)
      base = settings
      while base.respond_to?(:errors)
        args_array = base.errors[key]

        next base = base.superclass unless args_array

        args_array.reverse_each do |args|
          first = args == args_array.first
          args += [block_params]
          resp = process_route(*args)
          return resp unless resp.nil? && !first
        end
      end
      return false unless key.respond_to?(:superclass) && (key.superclass < Exception)

      error_block!(key.superclass, *block_params)
    end

    def dump_errors!(boom)
      if boom.respond_to?(:detailed_message)
        msg = boom.detailed_message(highlight: false)
        if msg =~ /\A(.*?)(?: \(#{ Regexp.quote(boom.class.to_s) }\))?\n/
          msg = $1
          additional_msg = $'.lines(chomp: true)
        else
          additional_msg = []
        end
      else
        msg = boom.message
        additional_msg = []
      end
      msg = ["#{Time.now.strftime('%Y-%m-%d %H:%M:%S')} - #{boom.class} - #{msg}:", *additional_msg, *boom.backtrace].join("\n\t")
      @env['rack.errors'].puts(msg)
    end

    class << self
      CALLERS_TO_IGNORE = [ # :nodoc:
        %r{/sinatra(/(base|main|show_exceptions))?\.rb$},   # all sinatra code
        %r{lib/tilt.*\.rb$},                                # all tilt code
        /^\(.*\)$/,                                         # generated code
        /\/bundled_gems.rb$/,                               # ruby >= 3.3 with bundler >= 2.5
        %r{rubygems/(custom|core_ext/kernel)_require\.rb$}, # rubygems require hacks
        /active_support/,                                   # active_support require hacks
        %r{bundler(/(?:runtime|inline))?\.rb},              # bundler require hacks
        /<internal:/,                                       # internal in ruby >= 1.9.2
        %r{zeitwerk/(core_ext/)?kernel\.rb}                 # Zeitwerk kernel#require decorator
      ].freeze

      attr_reader :routes, :filters, :templates, :errors, :on_start_callback, :on_stop_callback

      def callers_to_ignore
        CALLERS_TO_IGNORE
      end

      # Removes all routes, filters, middleware and extension hooks from the
      # current class (not routes/filters/... defined by its superclass).
      def reset!
        @conditions     = []
        @routes         = {}
        @filters        = { before: [], after: [] }
        @errors         = {}
        @middleware     = []
        @prototype      = nil
        @extensions     = []

        @templates = if superclass.respond_to?(:templates)
                       Hash.new { |_hash, key| superclass.templates[key] }
                     else
                       {}
                     end
      end

      # Extension modules registered on this class and all superclasses.
      def extensions
        if superclass.respond_to?(:extensions)
          (@extensions + superclass.extensions).uniq
        else
          @extensions
        end
      end

      # Middleware used in this class and all superclasses.
      def middleware
        if superclass.respond_to?(:middleware)
          superclass.middleware + @middleware
        else
          @middleware
        end
      end

      # Sets an option to the given value.  If the value is a proc,
      # the proc will be called every time the option is accessed.
      def set(option, value = (not_set = true), ignore_setter = false, &block)
        raise ArgumentError if block && !not_set

        if block
          value = block
          not_set = false
        end

        if not_set
          raise ArgumentError unless option.respond_to?(:each)

          option.each { |k, v| set(k, v) }
          return self
        end

        if respond_to?("#{option}=") && !ignore_setter
          return __send__("#{option}=", value)
        end

        setter = proc { |val| set option, val, true }
        getter = proc { value }

        case value
        when Proc
          getter = value
        when Symbol, Integer, FalseClass, TrueClass, NilClass
          getter = value.inspect
        when Hash
          setter = proc do |val|
            val = value.merge val if Hash === val
            set option, val, true
          end
        end

        define_singleton("#{option}=", setter)
        define_singleton(option, getter)
        define_singleton("#{option}?", "!!#{option}") unless method_defined? "#{option}?"
        self
      end

      # Same as calling `set :option, true` for each of the given options.
      def enable(*opts)
        opts.each { |key| set(key, true) }
      end

      # Same as calling `set :option, false` for each of the given options.
      def disable(*opts)
        opts.each { |key| set(key, false) }
      end

      # Define a custom error handler. Optionally takes either an Exception
      # class, or an HTTP status code to specify which errors should be
      # handled.
      def error(*codes, &block)
        args  = compile! 'ERROR', /.*/, block
        codes = codes.flat_map(&method(:Array))
        codes << Exception if codes.empty?
        codes << Sinatra::NotFound if codes.include?(404)
        codes.each { |c| (@errors[c] ||= []) << args }
      end

      # Sugar for `error(404) { ... }`
      def not_found(&block)
        error(404, &block)
      end

      # Define a named template. The block must return the template source.
      def template(name, &block)
        filename, line = caller_locations.first
        templates[name] = [block, filename, line.to_i]
      end

      # Define the layout template. The block must return the template source.
      def layout(name = :layout, &block)
        template name, &block
      end

      # Load embedded templates from the file; uses the caller's __FILE__
      # when no file is specified.
      def inline_templates=(file = nil)
        file = (caller_files.first || File.expand_path($0)) if file.nil? || file == true

        begin
          io = ::IO.respond_to?(:binread) ? ::IO.binread(file) : ::IO.read(file)
          app, data = io.gsub("\r\n", "\n").split(/^__END__$/, 2)
        rescue Errno::ENOENT
          app, data = nil
        end

        return unless data

        encoding = if app && app =~ /([^\n]*\n)?#[^\n]*coding: *(\S+)/m
                     $2
                   else
                     settings.default_encoding
                   end

        lines = app.count("\n") + 1
        template = nil
        force_encoding data, encoding
        data.each_line do |line|
          lines += 1
          if line =~ /^@@\s*(.*\S)\s*$/
            template = force_encoding(String.new, encoding)
            templates[$1.to_sym] = [template, file, lines]
          elsif template
            template << line
          end
        end
      end

      # Lookup or register a mime type in Rack's mime registry.
      def mime_type(type, value = nil)
        return type      if type.nil?
        return type.to_s if type.to_s.include?('/')

        type = ".#{type}" unless type.to_s[0] == '.'
        return Rack::Mime.mime_type(type, nil) unless value

        Rack::Mime::MIME_TYPES[type] = value
      end

      # provides all mime types matching type, including deprecated types:
      #   mime_types :html # => ['text/html']
      #   mime_types :js   # => ['application/javascript', 'text/javascript']
      def mime_types(type)
        type = mime_type type
        if type =~ %r{^application/(xml|javascript)$}
          [type, "text/#{$1}"]
        elsif type =~ %r{^text/(xml|javascript)$}
          [type, "application/#{$1}"]
        else
          [type]
        end
      end

      # Define a before filter; runs before all requests within the same
      # context as route handlers and may access/modify the request and
      # response.
      def before(path = /.*/, **options, &block)
        add_filter(:before, path, **options, &block)
      end

      # Define an after filter; runs after all requests within the same
      # context as route handlers and may access/modify the request and
      # response.
      def after(path = /.*/, **options, &block)
        add_filter(:after, path, **options, &block)
      end

      # add a filter
      def add_filter(type, path = /.*/, **options, &block)
        filters[type] << compile!(type, path, block, **options)
      end

      def on_start(&on_start_callback)
        @on_start_callback = on_start_callback
      end

      def on_stop(&on_stop_callback)
        @on_stop_callback = on_stop_callback
      end

      # Add a route condition. The route is considered non-matching when the
      # block returns false.
      def condition(name = "#{caller.first[/`.*'/]} condition", &block)
        @conditions << generate_method(name, &block)
      end

      def public=(value)
        warn_for_deprecation ':public is no longer used to avoid overloading Module#public, use :public_folder or :public_dir instead'
        set(:public_folder, value)
      end

      def public_dir=(value)
        self.public_folder = value
      end

      def public_dir
        public_folder
      end

      # Defining a `GET` handler also automatically defines
      # a `HEAD` handler.
      def get(path, opts = {}, &block)
        conditions = @conditions.dup
        route('GET', path, opts, &block)

        @conditions = conditions
        route('HEAD', path, opts, &block)
      end

      def put(path, opts = {}, &block)     route 'PUT',     path, opts, &block end

      def post(path, opts = {}, &block)    route 'POST',    path, opts, &block end

      def delete(path, opts = {}, &block)  route 'DELETE',  path, opts, &block end

      def head(path, opts = {}, &block)    route 'HEAD',    path, opts, &block end

      def options(path, opts = {}, &block) route 'OPTIONS', path, opts, &block end

      def patch(path, opts = {}, &block)   route 'PATCH',   path, opts, &block end

      def link(path, opts = {}, &block)    route 'LINK',    path, opts, &block end

      def unlink(path, opts = {}, &block)  route 'UNLINK',  path, opts, &block end

      # Makes the methods defined in the block and in the Modules given
      # in `extensions` available to the handlers and templates
      def helpers(*extensions, &block)
        class_eval(&block)   if block_given?
        include(*extensions) if extensions.any?
      end

      # Register an extension. Alternatively take a block from which an
      # extension will be created and registered on the fly.
      def register(*extensions, &block)
        extensions << Module.new(&block) if block_given?
        @extensions += extensions
        extensions.each do |extension|
          extend extension
          extension.registered(self) if extension.respond_to?(:registered)
        end
      end

      def development?; environment == :development end
      def production?;  environment == :production  end
      def test?;        environment == :test        end

      # Set configuration options for Sinatra and/or the app.
      # Allows scoping of settings for certain environments.
      def configure(*envs)
        yield self if envs.empty? || envs.include?(environment.to_sym)
      end

      # Use the specified Rack middleware
      def use(middleware, *args, &block)
        @prototype = nil
        @middleware << [middleware, args, block]
      end
      ruby2_keywords(:use) if respond_to?(:ruby2_keywords, true)

      # Stop the self-hosted server if running.
      def quit!
        return unless running?

        # Use Thin's hard #stop! if available, otherwise just #stop.
        running_server.respond_to?(:stop!) ? running_server.stop! : running_server.stop
        warn '== Sinatra has ended his set (crowd applauds)' unless suppress_messages?
        set :running_server, nil
        set :handler_name, nil

        on_stop_callback.call unless on_stop_callback.nil?
      end

      alias stop! quit!

      # Run the Sinatra app as a self-hosted server using
      # Puma, Falcon (in that order). If given a block, will call
      # with the constructed handler once we have taken the stage.
      def run!(options = {}, &block)
        unless defined?(Rackup::Handler)
          rackup_warning = <<~MISSING_RACKUP
            Sinatra could not start, the required gems weren't found!

            Add them to your bundle with:

                bundle add rackup puma

            or install them with:

                gem install rackup puma

          MISSING_RACKUP
          warn rackup_warning
          exit 1
        end

        return if running?

        set options
        handler         = Rackup::Handler.pick(server)
        handler_name    = handler.name.gsub(/.*::/, '')
        server_settings = settings.respond_to?(:server_settings) ? settings.server_settings : {}
        server_settings.merge!(Port: port, Host: bind)

        begin
          start_server(handler, server_settings, handler_name, &block)
        rescue Errno::EADDRINUSE
          warn "== Someone is already performing on port #{port}!"
          raise
        ensure
          quit!
        end
      end

      alias start! run!

      # Check whether the self-hosted server is running or not.
      def running?
        running_server?
      end

      # The prototype instance used to process requests.
      def prototype
        @prototype ||= new
      end

      # Create a new instance without middleware in front of it.
      alias new! new unless method_defined? :new!

      # Create a new instance of the class fronted by its middleware
      # pipeline. The object is guaranteed to respond to #call but may not be
      # an instance of the class new was called on.
      def new(*args, &block)
        instance = new!(*args, &block)
        Wrapper.new(build(instance).to_app, instance)
      end
      ruby2_keywords :new if respond_to?(:ruby2_keywords, true)

      # Creates a Rack::Builder instance with all the middleware set up and
      # the given +app+ as end point.
      def build(app)
        builder = Rack::Builder.new
        setup_default_middleware builder
        setup_middleware builder
        builder.run app
        builder
      end

      def call(env)
        synchronize { prototype.call(env) }
      end

      # Like Kernel#caller but excluding certain magic entries and without
      # line / method information; the resulting array contains filenames only.
      def caller_files
        cleaned_caller(1).flatten
      end

      private

      # Starts the server by running the Rack Handler.
      def start_server(handler, server_settings, handler_name)
        # Ensure we initialize middleware before startup, to match standard Rack
        # behavior, by ensuring an instance exists:
        prototype
        # Run the instance we created:
        handler.run(self, **server_settings) do |server|
          unless suppress_messages?
            warn "== Sinatra (v#{Sinatra::VERSION}) has taken the stage on #{port} for #{environment} with backup from #{handler_name}"
          end

          setup_traps
          set :running_server, server
          set :handler_name,   handler_name
          server.threaded = settings.threaded if server.respond_to? :threaded=
          on_start_callback.call unless on_start_callback.nil?
          yield server if block_given?
        end
      end

      def suppress_messages?
        handler_name =~ /cgi/i || quiet
      end

      def setup_traps
        return unless traps?

        at_exit { quit! }

        %i[INT TERM].each do |signal|
          old_handler = trap(signal) do
            quit!
            old_handler.call if old_handler.respond_to?(:call)
          end
        end

        set :traps, false
      end

      # Dynamically defines a method on settings.
      def define_singleton(name, content = Proc.new)
        singleton_class.class_eval do
          undef_method(name) if method_defined? name
          String === content ? class_eval("def #{name}() #{content}; end") : define_method(name, &content)
        end
      end

      # Condition for matching host name. Parameter might be String or Regexp.
      def host_name(pattern)
        condition { pattern === request.host }
      end

      # Condition for matching user agent. Parameter should be Regexp.
      # Will set params[:agent].
      def user_agent(pattern)
        condition do
          if request.user_agent.to_s =~ pattern
            @params[:agent] = $~[1..-1]
            true
          else
            false
          end
        end
      end
      alias agent user_agent

      # Condition for matching mimetypes. Accepts file extensions.
      def provides(*types)
        types.map! { |t| mime_types(t) }
        types.flatten!
        condition do
          response_content_type = response['content-type']
          preferred_type = request.preferred_type(types)

          if response_content_type
            types.include?(response_content_type) || types.include?(response_content_type[/^[^;]+/])
          elsif preferred_type
            params = (preferred_type.respond_to?(:params) ? preferred_type.params : {})
            content_type(preferred_type, params)
            true
          else
            false
          end
        end
      end

      def route(verb, path, options = {}, &block)
        enable :empty_path_info if path == '' && empty_path_info.nil?
        signature = compile!(verb, path, block, **options)
        (@routes[verb] ||= []) << signature
        invoke_hook(:route_added, verb, path, block)
        signature
      end

      def invoke_hook(name, *args)
        extensions.each { |e| e.send(name, *args) if e.respond_to?(name) }
      end

      def generate_method(method_name, &block)
        define_method(method_name, &block)
        method = instance_method method_name
        remove_method method_name
        method
      end

      def compile!(verb, path, block, **options)
        # Because of self.options.host
        host_name(options.delete(:host)) if options.key?(:host)
        # Pass Mustermann opts to compile()
        route_mustermann_opts = options.key?(:mustermann_opts) ? options.delete(:mustermann_opts) : {}.freeze

        options.each_pair { |option, args| send(option, *args) }

        pattern                 = compile(path, route_mustermann_opts)
        method_name             = "#{verb} #{path}"
        unbound_method          = generate_method(method_name, &block)
        conditions = @conditions
        @conditions = []
        wrapper = block.arity.zero? ?
          proc { |a, _p| unbound_method.bind(a).call } :
          proc { |a, p| unbound_method.bind(a).call(*p) }

        [pattern, conditions, wrapper]
      end

      def compile(path, route_mustermann_opts = {})
        Mustermann.new(path, **mustermann_opts.merge(route_mustermann_opts))
      end

      def setup_default_middleware(builder)
        builder.use ExtendedRack
        builder.use ShowExceptions       if show_exceptions?
        builder.use Rack::MethodOverride if method_override?
        builder.use Rack::Head
        setup_logging    builder
        setup_sessions   builder
        setup_protection builder
        setup_host_authorization builder
      end

      def setup_middleware(builder)
        middleware.each { |c, a, b| builder.use(c, *a, &b) }
      end

      def setup_logging(builder)
        if logging?
          setup_common_logger(builder)
          setup_custom_logger(builder)
        elsif logging == false
          setup_null_logger(builder)
        end
      end

      def setup_null_logger(builder)
        builder.use Sinatra::Middleware::Logger, ::Logger::FATAL
      end

      def setup_common_logger(builder)
        builder.use Sinatra::CommonLogger
      end

      def setup_custom_logger(builder)
        if logging.respond_to? :to_int
          builder.use Sinatra::Middleware::Logger, logging
        else
          builder.use Sinatra::Middleware::Logger
        end
      end

      def setup_protection(builder)
        return unless protection?

        options = Hash === protection ? protection.dup : {}
        options = {
          img_src: "'self' data:",
          font_src: "'self'"
        }.merge options

        protect_session = options.fetch(:session) { sessions? }
        options[:without_session] = !protect_session

        options[:reaction] ||= :drop_session

        builder.use Rack::Protection, options
      end

      def setup_host_authorization(builder)
        builder.use Rack::Protection::HostAuthorization, host_authorization
      end

      def setup_sessions(builder)
        return unless sessions?

        options = {}
        options[:secret] = session_secret if session_secret?
        options.merge! sessions.to_hash if sessions.respond_to? :to_hash
        builder.use session_store, options
      end

      def inherited(subclass)
        subclass.reset!
        subclass.set :app_file, caller_files.first unless subclass.app_file?
        super
      end

      @@mutex = Mutex.new
      def synchronize(&block)
        if lock?
          @@mutex.synchronize(&block)
        else
          yield
        end
      end

      # used for deprecation warnings
      def warn_for_deprecation(message)
        warn message + "\n\tfrom #{cleaned_caller.first.join(':')}"
      end

      # Like Kernel#caller but excluding certain magic entries
      def cleaned_caller(keep = 3)
        caller(1)
          .map! { |line| line.split(/:(?=\d|in )/, 3)[0, keep] }
          .reject { |file, *_| callers_to_ignore.any? { |pattern| file =~ pattern } }
      end
    end

    # Force data to specified encoding. It defaults to settings.default_encoding
    # which is UTF-8 by default
    def self.force_encoding(data, encoding = default_encoding)
      return if data == settings || data.is_a?(Tempfile)

      if data.respond_to? :force_encoding
        data.force_encoding(encoding).encode!
      elsif data.respond_to? :each_value
        data.each_value { |v| force_encoding(v, encoding) }
      elsif data.respond_to? :each
        data.each { |v| force_encoding(v, encoding) }
      end
      data
    end

    def force_encoding(*args)
      settings.force_encoding(*args)
    end

    reset!

    set :environment, (ENV['APP_ENV'] || ENV['RACK_ENV'] || :development).to_sym
    set :raise_errors, proc { test? }
    set :dump_errors, proc { !test? }
    set :show_exceptions, proc { development? }
    set :sessions, false
    set :session_store, Rack::Session::Cookie
    set :logging, false
    set :protection, true
    set :method_override, false
    set :use_code, false
    set :default_encoding, 'utf-8'
    set :x_cascade, true
    set :add_charset, %w[javascript xml xhtml+xml].map { |t| "application/#{t}" }
    settings.add_charset << %r{^text/}
    set :mustermann_opts, {}
    set :default_content_type, 'text/html'

    # explicitly generating a session secret eagerly to play nice with preforking
    begin
      require 'securerandom'
      set :session_secret, SecureRandom.hex(64)
    rescue LoadError, NotImplementedError, RuntimeError
      # SecureRandom raises a NotImplementedError if no random device is available
      # RuntimeError raised due to broken openssl backend: https://bugs.ruby-lang.org/issues/19230
      set :session_secret, format('%064x', Kernel.rand((2**256) - 1))
    end

    class << self
      alias methodoverride? method_override?
      alias methodoverride= method_override=
    end

    set :run, false                       # start server via at-exit hook?
    set :running_server, nil
    set :handler_name, nil
    set :traps, true
    set :server, %w[webrick]
    set :bind, proc { development? ? 'localhost' : '0.0.0.0' }
    set :port, Integer(ENV['PORT'] && !ENV['PORT'].empty? ? ENV['PORT'] : 4567)
    set :quiet, false
    set :host_authorization, ->() do
      if development?
        {
          permitted_hosts: [
            "localhost",
            ".localhost",
            ".test",
            IPAddr.new("0.0.0.0/0"),
            IPAddr.new("::/0"),
          ]
        }
      else
        {}
      end
    end

    ruby_engine = defined?(RUBY_ENGINE) && RUBY_ENGINE

    server.unshift 'thin'     if ruby_engine != 'jruby'
    server.unshift 'falcon'   if ruby_engine != 'jruby'
    server.unshift 'trinidad' if ruby_engine == 'jruby'
    server.unshift 'puma'

    set :absolute_redirects, true
    set :prefixed_redirects, false
    set :empty_path_info, nil
    set :strict_paths, true

    set :app_file, nil
    set :root, proc { app_file && File.expand_path(File.dirname(app_file)) }
    set :views, proc { root && File.join(root, 'views') }
    set :reload_templates, proc { development? }
    set :lock, false
    set :threaded, true

    set :public_folder, proc { root && File.join(root, 'public') }
    set :static, proc { public_folder && File.exist?(public_folder) }
    set :static_cache_control, false
    
    set :static_headers, {}

    error ::Exception do
      response.status = 500
      content_type 'text/html'
      '<h1>Internal Server Error</h1>'
    end

    configure :development do
      get '/__sinatra__/:image.png' do
        filename = __dir__ + "/images/#{params[:image].to_i}.png"
        content_type :png
        send_file filename
      end

      error NotFound do
        content_type 'text/html'

        if instance_of?(Sinatra::Application)
          code = <<-RUBY.gsub(/^ {12}/, '')
            #{request.request_method.downcase} '#{request.path_info}' do
              "Hello World"
            end
          RUBY
        else
          code = <<-RUBY.gsub(/^ {12}/, '')
            class #{self.class}
              #{request.request_method.downcase} '#{request.path_info}' do
                "Hello World"
              end
            end
          RUBY

          file = settings.app_file.to_s.sub(settings.root.to_s, '').sub(%r{^/}, '')
          code = "# in #{file}\n#{code}" unless file.empty?
        end

        <<-HTML.gsub(/^ {10}/, '')
          <!DOCTYPE html>
          <html>
          <head>
            <style type="text/css">
            body { text-align:center;font-family:helvetica,arial;font-size:22px;
              color:#888;margin:20px}
            #c {margin:0 auto;width:500px;text-align:left}
            </style>
          </head>
          <body>
            <h2>Sinatra doesn’t know this ditty.</h2>
            <img src='#{request.script_name}/__sinatra__/404.png'>
            <div id="c">
              Try this:
              <pre>#{Rack::Utils.escape_html(code)}</pre>
            </div>
          </body>
          </html>
        HTML
      end
    end
  end

  # Execution context for classic style (top-level) applications. All
  # DSL methods executed on main are delegated to this class.
  #
  # The Application class should not be subclassed, unless you want to
  # inherit all settings, routes, handlers, and error pages from the
  # top-level. Subclassing Sinatra::Base is highly recommended for
  # modular applications.
  class Application < Base
    set :logging, proc { !test? }
    set :method_override, true
    set :run, proc { !test? }
    set :app_file, nil

    def self.register(*extensions, &block) # :nodoc:
      added_methods = extensions.flat_map(&:public_instance_methods)
      Delegator.delegate(*added_methods)
      super(*extensions, &block)
    end
  end

  # Sinatra delegation mixin. Mixing this module into an object causes all
  # methods to be delegated to the Sinatra::Application class. Used primarily
  # at the top-level.
  module Delegator # :nodoc:
    def self.delegate(*methods)
      methods.each do |method_name|
        next if method_defined?(method_name) || private_method_defined?(method_name)

        define_method(method_name) do |*args, &block|
          return super(*args, &block) if respond_to? method_name

          Delegator.target.send(method_name, *args, &block)
        end
        # ensure keyword argument passing is compatible with ruby >= 2.7
        ruby2_keywords(method_name) if respond_to?(:ruby2_keywords, true)
        private method_name
      end
    end

    delegate :get, :patch, :put, :post, :delete, :head, :options, :link, :unlink,
             :template, :layout, :before, :after, :error, :not_found, :configure,
             :set, :mime_type, :enable, :disable, :use, :development?, :test?,
             :production?, :helpers, :settings, :register, :on_start, :on_stop

    class << self
      attr_accessor :target
    end

    self.target = Application
  end

  class Wrapper
    def initialize(stack, instance)
      @stack = stack
      @instance = instance
    end

    def settings
      @instance.settings
    end

    def helpers
      @instance
    end

    def call(env)
      @stack.call(env)
    end

    def inspect
      "#<#{@instance.class} app_file=#{settings.app_file.inspect}>"
    end
  end

  # Create a new Sinatra application; the block is evaluated in the class scope.
  def self.new(base = Base, &block)
    base = Class.new(base)
    base.class_eval(&block) if block_given?
    base
  end

  # Extend the top-level DSL with the modules provided.
  def self.register(*extensions, &block)
    Delegator.target.register(*extensions, &block)
  end

  # Include the helper modules provided in Sinatra's request context.
  def self.helpers(*extensions, &block)
    Delegator.target.helpers(*extensions, &block)
  end

  # Use the middleware for classic applications.
  def self.use(*args, &block)
    Delegator.target.use(*args, &block)
  end
end


// --- rb_dependencies.rb ---
# frozen_string_literal: true

require "set"
require "active_support/dependencies/interlock"

module ActiveSupport # :nodoc:
  module Dependencies # :nodoc:
    require_relative "dependencies/require_dependency"

    singleton_class.attr_accessor :interlock
    @interlock = Interlock.new

    # :doc:

    # Execute the supplied block without interference from any
    # concurrent loads.
    def self.run_interlock(&block)
      interlock.running(&block)
    end

    # Execute the supplied block while holding an exclusive lock,
    # preventing any other thread from being inside a #run_interlock
    # block at the same time.
    def self.load_interlock(&block)
      interlock.loading(&block)
    end

    # Execute the supplied block while holding an exclusive lock,
    # preventing any other thread from being inside a #run_interlock
    # block at the same time.
    def self.unload_interlock(&block)
      interlock.unloading(&block)
    end

    # :nodoc:

    # The array of directories from which we autoload and reload, if reloading
    # is enabled. The public interface to push directories to this collection
    # from applications or engines is config.autoload_paths.
    #
    # This collection is allowed to have intersection with autoload_once_paths.
    # Common directories are not reloaded.
    singleton_class.attr_accessor :autoload_paths
    self.autoload_paths = []

    # The array of directories from which we autoload and never reload, even if
    # reloading is enabled. The public interface to push directories to this
    # collection from applications or engines is config.autoload_once_paths.
    singleton_class.attr_accessor :autoload_once_paths
    self.autoload_once_paths = []

    # This is a private set that collects all eager load paths during bootstrap.
    # Useful for Zeitwerk integration. The public interface to push custom
    # directories to this collection from applications or engines is
    # config.eager_load_paths.
    singleton_class.attr_accessor :_eager_load_paths
    self._eager_load_paths = Set.new

    # If reloading is enabled, this private set holds autoloaded classes tracked
    # by the descendants tracker. It is populated by an on_load callback in the
    # main autoloader. Used to clear state.
    singleton_class.attr_accessor :_autoloaded_tracked_classes
    self._autoloaded_tracked_classes = Set.new

    # If reloading is enabled, this private attribute stores the main autoloader
    # of a Rails application. It is `nil` otherwise.
    #
    # The public interface for this autoloader is `Rails.autoloaders.main`.
    singleton_class.attr_accessor :autoloader

    # Private method that reloads constants autoloaded by the main autoloader.
    #
    # Rails.application.reloader.reload! is the public interface for application
    # reload. That involves more things, like deleting unloaded classes from the
    # internal state of the descendants tracker, or reloading routes.
    def self.clear
      unload_interlock do
        _autoloaded_tracked_classes.clear
        autoloader.reload
      end
    end

    # Private method used by require_dependency.
    def self.search_for_file(relpath)
      relpath += ".rb" unless relpath.end_with?(".rb")
      autoload_paths.each do |autoload_path|
        abspath = File.join(autoload_path, relpath)
        return abspath if File.file?(abspath)
      end
      nil
    end

    # Private method that helps configuring the autoloaders.
    def self.eager_load?(path)
      _eager_load_paths.member?(path)
    end
  end
end


// --- rb_action_mailer_base.rb ---
# frozen_string_literal: true

require "mail"
require "action_mailer/collector"
require "active_support/core_ext/string/inflections"
require "active_support/core_ext/hash/except"
require "active_support/core_ext/module/anonymous"

require "action_mailer/log_subscriber"
require "action_mailer/rescuable"

module ActionMailer
  # = Action Mailer \Base
  #
  # Action Mailer allows you to send email from your application using a mailer model and views.
  #
  # == Mailer Models
  #
  # To use Action Mailer, you need to create a mailer model.
  #
  #   $ bin/rails generate mailer Notifier
  #
  # The generated model inherits from <tt>ApplicationMailer</tt> which in turn
  # inherits from +ActionMailer::Base+. A mailer model defines methods
  # used to generate an email message. In these methods, you can set up variables to be used in
  # the mailer views, options on the mail itself such as the <tt>:from</tt> address, and attachments.
  #
  #   class ApplicationMailer < ActionMailer::Base
  #     default from: 'from@example.com'
  #     layout 'mailer'
  #   end
  #
  #   class NotifierMailer < ApplicationMailer
  #     default from: 'no-reply@example.com',
  #             return_path: 'system@example.com'
  #
  #     def welcome(recipient)
  #       @account = recipient
  #       mail(to: recipient.email_address_with_name,
  #            bcc: ["bcc@example.com", "Order Watcher <watcher@example.com>"])
  #     end
  #   end
  #
  # Within the mailer method, you have access to the following methods:
  #
  # * <tt>attachments[]=</tt> - Allows you to add attachments to your email in an intuitive
  #   manner; <tt>attachments['filename.png'] = File.read('path/to/filename.png')</tt>
  #
  # * <tt>attachments.inline[]=</tt> - Allows you to add an inline attachment to your email
  #   in the same manner as <tt>attachments[]=</tt>
  #
  # * <tt>headers[]=</tt> - Allows you to specify any header field in your email such
  #   as <tt>headers['X-No-Spam'] = 'True'</tt>. Note that declaring a header multiple times
  #   will add many fields of the same name. Read #headers doc for more information.
  #
  # * <tt>headers(hash)</tt> - Allows you to specify multiple headers in your email such
  #   as <tt>headers({'X-No-Spam' => 'True', 'In-Reply-To' => '1234@message.id'})</tt>
  #
  # * <tt>mail</tt> - Allows you to specify email to be sent.
  #
  # The hash passed to the mail method allows you to specify any header that a +Mail::Message+
  # will accept (any valid email header including optional fields).
  #
  # The +mail+ method, if not passed a block, will inspect your views and send all the views with
  # the same name as the method, so the above action would send the +welcome.text.erb+ view
  # file as well as the +welcome.html.erb+ view file in a +multipart/alternative+ email.
  #
  # If you want to explicitly render only certain templates, pass a block:
  #
  #   mail(to: user.email) do |format|
  #     format.text
  #     format.html
  #   end
  #
  # The block syntax is also useful in providing information specific to a part:
  #
  #   mail(to: user.email) do |format|
  #     format.text(content_transfer_encoding: "base64")
  #     format.html
  #   end
  #
  # Or even to render a special view:
  #
  #   mail(to: user.email) do |format|
  #     format.text
  #     format.html { render "some_other_template" }
  #   end
  #
  # == Mailer views
  #
  # Like Action Controller, each mailer class has a corresponding view directory in which each
  # method of the class looks for a template with its name.
  #
  # To define a template to be used with a mailer, create an <tt>.erb</tt> file with the same
  # name as the method in your mailer model. For example, in the mailer defined above, the template at
  # <tt>app/views/notifier_mailer/welcome.text.erb</tt> would be used to generate the email.
  #
  # Variables defined in the methods of your mailer model are accessible as instance variables in their
  # corresponding view.
  #
  # Emails by default are sent in plain text, so a sample view for our model example might look like this:
  #
  #   Hi <%= @account.name %>,
  #   Thanks for joining our service! Please check back often.
  #
  # You can even use Action View helpers in these views. For example:
  #
  #   You got a new note!
  #   <%= truncate(@note.body, length: 25) %>
  #
  # If you need to access the subject, from, or the recipients in the view, you can do that through message object:
  #
  #   You got a new note from <%= message.from %>!
  #   <%= truncate(@note.body, length: 25) %>
  #
  #
  # == Generating URLs
  #
  # URLs can be generated in mailer views using <tt>url_for</tt> or named routes. Unlike controllers from
  # Action Pack, the mailer instance doesn't have any context about the incoming request, so you'll need
  # to provide all of the details needed to generate a URL.
  #
  # When using <tt>url_for</tt> you'll need to provide the <tt>:host</tt>, <tt>:controller</tt>, and <tt>:action</tt>:
  #
  #   <%= url_for(host: "example.com", controller: "welcome", action: "greeting") %>
  #
  # When using named routes you only need to supply the <tt>:host</tt>:
  #
  #   <%= users_url(host: "example.com") %>
  #
  # You should use the <tt>named_route_url</tt> style (which generates absolute URLs) and avoid using the
  # <tt>named_route_path</tt> style (which generates relative URLs), since clients reading the mail will
  # have no concept of a current URL from which to determine a relative path.
  #
  # It is also possible to set a default host that will be used in all mailers by setting the <tt>:host</tt>
  # option as a configuration option in <tt>config/application.rb</tt>:
  #
  #   config.action_mailer.default_url_options = { host: "example.com" }
  #
  # You can also define a <tt>default_url_options</tt> method on individual mailers to override these
  # default settings per-mailer.
  #
  # By default when <tt>config.force_ssl</tt> is +true+, URLs generated for hosts will use the HTTPS protocol.
  #
  # == Sending mail
  #
  # Once a mailer action and template are defined, you can deliver your message or defer its creation and
  # delivery for later:
  #
  #   NotifierMailer.welcome(User.first).deliver_now # sends the email
  #   mail = NotifierMailer.welcome(User.first)      # => an ActionMailer::MessageDelivery object
  #   mail.deliver_now                               # generates and sends the email now
  #
  # The ActionMailer::MessageDelivery class is a wrapper around a delegate that will call
  # your method to generate the mail. If you want direct access to the delegator, or +Mail::Message+,
  # you can call the <tt>message</tt> method on the ActionMailer::MessageDelivery object.
  #
  #   NotifierMailer.welcome(User.first).message     # => a Mail::Message object
  #
  # Action Mailer is nicely integrated with Active Job so you can generate and send emails in the background
  # (example: outside of the request-response cycle, so the user doesn't have to wait on it):
  #
  #   NotifierMailer.welcome(User.first).deliver_later # enqueue the email sending to Active Job
  #
  # Note that <tt>deliver_later</tt> will execute your method from the background job.
  #
  # You never instantiate your mailer class. Rather, you just call the method you defined on the class itself.
  # All instance methods are expected to return a message object to be sent.
  #
  # == Multipart Emails
  #
  # Multipart messages can also be used implicitly because Action Mailer will automatically detect and use
  # multipart templates, where each template is named after the name of the action, followed by the content
  # type. Each such detected template will be added to the message, as a separate part.
  #
  # For example, if the following templates exist:
  # * signup_notification.text.erb
  # * signup_notification.html.erb
  # * signup_notification.xml.builder
  # * signup_notification.yml.erb
  #
  # Each would be rendered and added as a separate part to the message, with the corresponding content
  # type. The content type for the entire message is automatically set to <tt>multipart/alternative</tt>,
  # which indicates that the email contains multiple different representations of the same email
  # body. The same instance variables defined in the action are passed to all email templates.
  #
  # Implicit template rendering is not performed if any attachments or parts have been added to the email.
  # This means that you'll have to manually add each part to the email and set the content type of the email
  # to <tt>multipart/alternative</tt>.
  #
  # == Attachments
  #
  # Sending attachment in emails is easy:
  #
  #   class NotifierMailer < ApplicationMailer
  #     def welcome(recipient)
  #       attachments['free_book.pdf'] = File.read('path/to/file.pdf')
  #       mail(to: recipient, subject: "New account information")
  #     end
  #   end
  #
  # Which will (if it had both a <tt>welcome.text.erb</tt> and <tt>welcome.html.erb</tt>
  # template in the view directory), send a complete <tt>multipart/mixed</tt> email with two parts,
  # the first part being a <tt>multipart/alternative</tt> with the text and HTML email parts inside,
  # and the second being a <tt>application/pdf</tt> with a Base64 encoded copy of the file.pdf book
  # with the filename +free_book.pdf+.
  #
  # If you need to send attachments with no content, you need to create an empty view for it,
  # or add an empty body parameter like this:
  #
  #     class NotifierMailer < ApplicationMailer
  #       def welcome(recipient)
  #         attachments['free_book.pdf'] = File.read('path/to/file.pdf')
  #         mail(to: recipient, subject: "New account information", body: "")
  #       end
  #     end
  #
  # You can also send attachments with HTML template, in this case you need to add body, attachments,
  # and custom content type like this:
  #
  #     class NotifierMailer < ApplicationMailer
  #       def welcome(recipient)
  #         attachments["free_book.pdf"] = File.read("path/to/file.pdf")
  #         mail(to: recipient,
  #              subject: "New account information",
  #              content_type: "text/html",
  #              body: "<html><body>Hello there</body></html>")
  #       end
  #     end
  #
  # == Inline Attachments
  #
  # You can also specify that a file should be displayed inline with other HTML. This is useful
  # if you want to display a corporate logo or a photo.
  #
  #   class NotifierMailer < ApplicationMailer
  #     def welcome(recipient)
  #       attachments.inline['photo.png'] = File.read('path/to/photo.png')
  #       mail(to: recipient, subject: "Here is what we look like")
  #     end
  #   end
  #
  # And then to reference the image in the view, you create a <tt>welcome.html.erb</tt> file and
  # make a call to +image_tag+ passing in the attachment you want to display and then call
  # +url+ on the attachment to get the relative content id path for the image source:
  #
  #   <h1>Please Don't Cringe</h1>
  #
  #   <%= image_tag attachments['photo.png'].url -%>
  #
  # As we are using Action View's +image_tag+ method, you can pass in any other options you want:
  #
  #   <h1>Please Don't Cringe</h1>
  #
  #   <%= image_tag attachments['photo.png'].url, alt: 'Our Photo', class: 'photo' -%>
  #
  # == Observing and Intercepting Mails
  #
  # Action Mailer provides hooks into the Mail observer and interceptor methods. These allow you to
  # register classes that are called during the mail delivery life cycle.
  #
  # An observer class must implement the <tt>:delivered_email(message)</tt> method which will be
  # called once for every email sent after the email has been sent.
  #
  # An interceptor class must implement the <tt>:delivering_email(message)</tt> method which will be
  # called before the email is sent, allowing you to make modifications to the email before it hits
  # the delivery agents. Your class should make any needed modifications directly to the passed
  # in +Mail::Message+ instance.
  #
  # == Default \Hash
  #
  # Action Mailer provides some intelligent defaults for your emails, these are usually specified in a
  # default method inside the class definition:
  #
  #   class NotifierMailer < ApplicationMailer
  #     default sender: 'system@example.com'
  #   end
  #
  # You can pass in any header value that a +Mail::Message+ accepts. Out of the box,
  # +ActionMailer::Base+ sets the following:
  #
  # * <tt>mime_version: "1.0"</tt>
  # * <tt>charset:      "UTF-8"</tt>
  # * <tt>content_type: "text/plain"</tt>
  # * <tt>parts_order:  [ "text/plain", "text/enriched", "text/html" ]</tt>
  #
  # <tt>parts_order</tt> and <tt>charset</tt> are not actually valid +Mail::Message+ header fields,
  # but Action Mailer translates them appropriately and sets the correct values.
  #
  # As you can pass in any header, you need to either quote the header as a string, or pass it in as
  # an underscored symbol, so the following will work:
  #
  #   class NotifierMailer < ApplicationMailer
  #     default 'Content-Transfer-Encoding' => '7bit',
  #             content_description: 'This is a description'
  #   end
  #
  # Finally, Action Mailer also supports passing <tt>Proc</tt> and <tt>Lambda</tt> objects into the default hash,
  # so you can define methods that evaluate as the message is being generated:
  #
  #   class NotifierMailer < ApplicationMailer
  #     default 'X-Special-Header' => Proc.new { my_method }, to: -> { @inviter.email_address }
  #
  #     private
  #       def my_method
  #         'some complex call'
  #       end
  #   end
  #
  # Note that the proc/lambda is evaluated right at the start of the mail message generation, so if you
  # set something in the default hash using a proc, and then set the same thing inside of your
  # mailer method, it will get overwritten by the mailer method.
  #
  # It is also possible to set these default options that will be used in all mailers through
  # the <tt>default_options=</tt> configuration in <tt>config/application.rb</tt>:
  #
  #    config.action_mailer.default_options = { from: "no-reply@example.org" }
  #
  # == \Callbacks
  #
  # You can specify callbacks using <tt>before_action</tt> and <tt>after_action</tt> for configuring your messages,
  # and using <tt>before_deliver</tt> and <tt>after_deliver</tt> for wrapping the delivery process.
  # For example, when you want to add default inline attachments and log delivery for all messages
  # sent out by a certain mailer class:
  #
  #   class NotifierMailer < ApplicationMailer
  #     before_action :add_inline_attachment!
  #     after_deliver :log_delivery
  #
  #     def welcome
  #       mail
  #     end
  #
  #     private
  #       def add_inline_attachment!
  #         attachments.inline["footer.jpg"] = File.read('/path/to/filename.jpg')
  #       end
  #
  #       def log_delivery
  #         Rails.logger.info "Sent email with message id '#{message.message_id}' at #{Time.current}."
  #       end
  #   end
  #
  # Action callbacks in Action Mailer are implemented using
  # AbstractController::Callbacks, so you can define and configure
  # callbacks in the same manner that you would use callbacks in classes that
  # inherit from ActionController::Base.
  #
  # Note that unless you have a specific reason to do so, you should prefer
  # using <tt>before_action</tt> rather than <tt>after_action</tt> in your
  # Action Mailer classes so that headers are parsed properly.
  #
  # == Rescuing Errors
  #
  # +rescue+ blocks inside of a mailer method cannot rescue errors that occur
  # outside of rendering -- for example, record deserialization errors in a
  # background job, or errors from a third-party mail delivery service.
  #
  # To rescue errors that occur during any part of the mailing process, use
  # {rescue_from}[rdoc-ref:ActiveSupport::Rescuable::ClassMethods#rescue_from]:
  #
  #   class NotifierMailer < ApplicationMailer
  #     rescue_from ActiveJob::DeserializationError do
  #       # ...
  #     end
  #
  #     rescue_from "SomeThirdPartyService::ApiError" do
  #       # ...
  #     end
  #
  #     def notify(recipient)
  #       mail(to: recipient, subject: "Notification")
  #     end
  #   end
  #
  # == Previewing emails
  #
  # You can preview your email templates visually by adding a mailer preview file to the
  # <tt>ActionMailer::Base.preview_paths</tt>. Since most emails do something interesting
  # with database data, you'll need to write some scenarios to load messages with fake data:
  #
  #   class NotifierMailerPreview < ActionMailer::Preview
  #     def welcome
  #       NotifierMailer.welcome(User.first)
  #     end
  #   end
  #
  # Methods must return a +Mail::Message+ object which can be generated by calling the mailer
  # method without the additional <tt>deliver_now</tt> / <tt>deliver_later</tt>. The location of the
  # mailer preview directories can be configured using the <tt>preview_paths</tt> option which has a default
  # of <tt>test/mailers/previews</tt>:
  #
  #   config.action_mailer.preview_paths << "#{Rails.root}/lib/mailer_previews"
  #
  # An overview of all previews is accessible at <tt>http://localhost:3000/rails/mailers</tt>
  # on a running development server instance.
  #
  # Previews can also be intercepted in a similar manner as deliveries can be by registering
  # a preview interceptor that has a <tt>previewing_email</tt> method:
  #
  #   class CssInlineStyler
  #     def self.previewing_email(message)
  #       # inline CSS styles
  #     end
  #   end
  #
  #   config.action_mailer.preview_interceptors :css_inline_styler
  #
  # Note that interceptors need to be registered both with <tt>register_interceptor</tt>
  # and <tt>register_preview_interceptor</tt> if they should operate on both sending and
  # previewing emails.
  #
  # == Configuration options
  #
  # These options are specified on the class level, like
  # <tt>ActionMailer::Base.raise_delivery_errors = true</tt>
  #
  # * <tt>default_options</tt> - You can pass this in at a class level as well as within the class itself as
  #   per the above section.
  #
  # * <tt>logger</tt> - the logger is used for generating information on the mailing run if available.
  #   Can be set to +nil+ for no logging. Compatible with both Ruby's own +Logger+ and Log4r loggers.
  #
  # * <tt>smtp_settings</tt> - Allows detailed configuration for <tt>:smtp</tt> delivery method:
  #   * <tt>:address</tt> - Allows you to use a remote mail server. Just change it from its default
  #     "localhost" setting.
  #   * <tt>:port</tt> - On the off chance that your mail server doesn't run on port 25, you can change it.
  #   * <tt>:domain</tt> - If you need to specify a HELO domain, you can do it here.
  #   * <tt>:user_name</tt> - If your mail server requires authentication, set the username in this setting.
  #   * <tt>:password</tt> - If your mail server requires authentication, set the password in this setting.
  #   * <tt>:authentication</tt> - If your mail server requires authentication, you need to specify the
  #     authentication type here.
  #     This is a symbol and one of <tt>:plain</tt> (will send the password Base64 encoded), <tt>:login</tt> (will
  #     send the password Base64 encoded) or <tt>:cram_md5</tt> (combines a Challenge/Response mechanism to exchange
  #     information and a cryptographic Message Digest 5 algorithm to hash important information)
  #   * <tt>:enable_starttls</tt> - Use STARTTLS when connecting to your SMTP server and fail if unsupported. Defaults
  #     to <tt>false</tt>. Requires at least version 2.7 of the Mail gem.
  #   * <tt>:enable_starttls_auto</tt> - Detects if STARTTLS is enabled in your SMTP server and starts
  #     to use it. Defaults to <tt>true</tt>.
  #   * <tt>:openssl_verify_mode</tt> - When using TLS, you can set how OpenSSL checks the certificate. This is
  #     really useful if you need to validate a self-signed and/or a wildcard certificate. You can use the name
  #     of an OpenSSL verify constant (<tt>'none'</tt> or <tt>'peer'</tt>) or directly the constant
  #     (+OpenSSL::SSL::VERIFY_NONE+ or +OpenSSL::SSL::VERIFY_PEER+).
  #   * <tt>:ssl/:tls</tt> Enables the SMTP connection to use SMTP/TLS (SMTPS: SMTP over direct TLS connection)
  #   * <tt>:open_timeout</tt> Number of seconds to wait while attempting to open a connection.
  #   * <tt>:read_timeout</tt> Number of seconds to wait until timing-out a read(2) call.
  #
  # * <tt>sendmail_settings</tt> - Allows you to override options for the <tt>:sendmail</tt> delivery method.
  #   * <tt>:location</tt> - The location of the sendmail executable. Defaults to <tt>/usr/sbin/sendmail</tt>.
  #   * <tt>:arguments</tt> - The command line arguments. Defaults to <tt>%w[ -i ]</tt> with <tt>-f sender@address</tt>
  #     added automatically before the message is sent.
  #
  # * <tt>file_settings</tt> - Allows you to override options for the <tt>:file</tt> delivery method.
  #   * <tt>:location</tt> - The directory into which emails will be written. Defaults to the application
  #     <tt>tmp/mails</tt>.
  #
  # * <tt>raise_delivery_errors</tt> - Whether or not errors should be raised if the email fails to be delivered.
  #
  # * <tt>delivery_method</tt> - Defines a delivery method. Possible values are <tt>:smtp</tt> (default),
  #   <tt>:sendmail</tt>, <tt>:test</tt>, and <tt>:file</tt>. Or you may provide a custom delivery method
  #   object e.g. +MyOwnDeliveryMethodClass+. See the Mail gem documentation on the interface you need to
  #   implement for a custom delivery agent.
  #
  # * <tt>perform_deliveries</tt> - Determines whether emails are actually sent from Action Mailer when you
  #   call <tt>.deliver</tt> on an email message or on an Action Mailer method. This is on by default but can
  #   be turned off to aid in functional testing.
  #
  # * <tt>deliveries</tt> - Keeps an array of all the emails sent out through the Action Mailer with
  #   <tt>delivery_method :test</tt>. Most useful for unit and functional testing.
  #
  # * <tt>delivery_job</tt> - The job class used with <tt>deliver_later</tt>. Mailers can set this to use a
  #   custom delivery job. Defaults to +ActionMailer::MailDeliveryJob+.
  #
  # * <tt>deliver_later_queue_name</tt> - The queue name used by <tt>deliver_later</tt> with the default
  #   <tt>delivery_job</tt>. Mailers can set this to use a custom queue name.
  class Base < AbstractController::Base
    include Callbacks
    include DeliveryMethods
    include QueuedDelivery
    include Rescuable
    include Parameterized
    include Previews
    include FormBuilder

    abstract!

    include AbstractController::Rendering

    include AbstractController::Logger
    include AbstractController::Helpers
    include AbstractController::Translation
    include AbstractController::AssetPaths
    include AbstractController::Callbacks
    include AbstractController::Caching

    include ActionView::Layouts

    PROTECTED_IVARS = AbstractController::Rendering::DEFAULT_PROTECTED_INSTANCE_VARIABLES + [:@_action_has_layout]

    helper ActionMailer::MailHelper

    class_attribute :default_params, default: {
      mime_version: "1.0",
      charset:      "UTF-8",
      content_type: "text/plain",
      parts_order:  [ "text/plain", "text/enriched", "text/html" ]
    }.freeze

    class << self
      # Register one or more Observers which will be notified when mail is delivered.
      def register_observers(*observers)
        observers.flatten.compact.each { |observer| register_observer(observer) }
      end

      # Unregister one or more previously registered Observers.
      def unregister_observers(*observers)
        observers.flatten.compact.each { |observer| unregister_observer(observer) }
      end

      # Register one or more Interceptors which will be called before mail is sent.
      def register_interceptors(*interceptors)
        interceptors.flatten.compact.each { |interceptor| register_interceptor(interceptor) }
      end

      # Unregister one or more previously registered Interceptors.
      def unregister_interceptors(*interceptors)
        interceptors.flatten.compact.each { |interceptor| unregister_interceptor(interceptor) }
      end

      # Register an Observer which will be notified when mail is delivered.
      # Either a class, string, or symbol can be passed in as the Observer.
      # If a string or symbol is passed in it will be camelized and constantized.
      def register_observer(observer)
        Mail.register_observer(observer_class_for(observer))
      end

      # Unregister a previously registered Observer.
      # Either a class, string, or symbol can be passed in as the Observer.
      # If a string or symbol is passed in it will be camelized and constantized.
      def unregister_observer(observer)
        Mail.unregister_observer(observer_class_for(observer))
      end

      # Register an Interceptor which will be called before mail is sent.
      # Either a class, string, or symbol can be passed in as the Interceptor.
      # If a string or symbol is passed in it will be camelized and constantized.
      def register_interceptor(interceptor)
        Mail.register_interceptor(observer_class_for(interceptor))
      end

      # Unregister a previously registered Interceptor.
      # Either a class, string, or symbol can be passed in as the Interceptor.
      # If a string or symbol is passed in it will be camelized and constantized.
      def unregister_interceptor(interceptor)
        Mail.unregister_interceptor(observer_class_for(interceptor))
      end

      def observer_class_for(value) # :nodoc:
        case value
        when String, Symbol
          value.to_s.camelize.constantize
        else
          value
        end
      end
      private :observer_class_for

      # Returns the name of the current mailer. This method is also being used as a path for a view lookup.
      # If this is an anonymous mailer, this method will return +anonymous+ instead.
      def mailer_name
        @mailer_name ||= anonymous? ? "anonymous" : name.underscore
      end
      # Allows to set the name of current mailer.
      attr_writer :mailer_name
      alias :controller_path :mailer_name

      # Sets the defaults through app configuration:
      #
      #     config.action_mailer.default(from: "no-reply@example.org")
      #
      # Aliased by ::default_options=
      def default(value = nil)
        self.default_params = default_params.merge(value).freeze if value
        default_params
      end
      # Allows to set defaults through app configuration:
      #
      #    config.action_mailer.default_options = { from: "no-reply@example.org" }
      alias :default_options= :default

      # Wraps an email delivery inside of ActiveSupport::Notifications instrumentation.
      #
      # This method is actually called by the +Mail::Message+ object itself
      # through a callback when you call <tt>:deliver</tt> on the +Mail::Message+,
      # calling +deliver_mail+ directly and passing a +Mail::Message+ will do
      # nothing except tell the logger you sent the email.
      def deliver_mail(mail) # :nodoc:
        ActiveSupport::Notifications.instrument("deliver.action_mailer") do |payload|
          set_payload_for_mail(payload, mail)
          yield # Let Mail do the delivery actions
        end
      end

      # Returns an email in the format "Name <email@example.com>".
      #
      # If the name is a blank string, it returns just the address.
      def email_address_with_name(address, name)
        Mail::Address.new.tap do |builder|
          builder.address = address
          builder.display_name = name.presence
        end.to_s
      end

    private
      def set_payload_for_mail(payload, mail)
        payload[:mail]               = mail.encoded
        payload[:mailer]             = name
        payload[:message_id]         = mail.message_id
        payload[:subject]            = mail.subject
        payload[:to]                 = mail.to
        payload[:from]               = mail.from
        payload[:bcc]                = mail.bcc if mail.bcc.present?
        payload[:cc]                 = mail.cc  if mail.cc.present?
        payload[:date]               = mail.date
        payload[:perform_deliveries] = mail.perform_deliveries
      end

      def method_missing(method_name, *args)
        if action_methods.include?(method_name.to_s)
          MessageDelivery.new(self, method_name, *args)
        else
          super
        end
      end
      ruby2_keywords(:method_missing)

      def respond_to_missing?(method, include_all = false)
        action_methods.include?(method.to_s) || super
      end
    end

    attr_internal :message

    def initialize
      super()
      @_mail_was_called = false
      @_message = Mail.new
    end

    def process(method_name, *args) # :nodoc:
      payload = {
        mailer: self.class.name,
        action: method_name,
        args: args
      }

      ActiveSupport::Notifications.instrument("process.action_mailer", payload) do
        super
        @_message = NullMail.new unless @_mail_was_called
      end
    end
    ruby2_keywords(:process)

    class NullMail # :nodoc:
      def body; "" end
      def header; {} end

      def respond_to?(string, include_all = false)
        true
      end

      def method_missing(*args)
        nil
      end
    end

    # Returns the name of the mailer object.
    def mailer_name
      self.class.mailer_name
    end

    # Returns an email in the format "Name <email@example.com>".
    #
    # If the name is a blank string, it returns just the address.
    def email_address_with_name(address, name)
      self.class.email_address_with_name(address, name)
    end

    # Allows you to pass random and unusual headers to the new +Mail::Message+
    # object which will add them to itself.
    #
    #   headers['X-Special-Domain-Specific-Header'] = "SecretValue"
    #
    # You can also pass a hash into headers of header field names and values,
    # which will then be set on the +Mail::Message+ object:
    #
    #   headers 'X-Special-Domain-Specific-Header' => "SecretValue",
    #           'In-Reply-To' => incoming.message_id
    #
    # The resulting +Mail::Message+ will have the following in its header:
    #
    #   X-Special-Domain-Specific-Header: SecretValue
    #
    # Note about replacing already defined headers:
    #
    # * +subject+
    # * +sender+
    # * +from+
    # * +to+
    # * +cc+
    # * +bcc+
    # * +reply-to+
    # * +orig-date+
    # * +message-id+
    # * +references+
    #
    # Fields can only appear once in email headers while other fields such as
    # <tt>X-Anything</tt> can appear multiple times.
    #
    # If you want to replace any header which already exists, first set it to
    # +nil+ in order to reset the value otherwise another field will be added
    # for the same header.
    def headers(args = nil)
      if args
        @_message.headers(args)
      else
        @_message
      end
    end

    # Allows you to add attachments to an email, like so:
    #
    #  mail.attachments['filename.jpg'] = File.read('/path/to/filename.jpg')
    #
    # If you do this, then Mail will take the file name and work out the mime type.
    # It will also set the +Content-Type+, +Content-Disposition+, and +Content-Transfer-Encoding+,
    # and encode the contents of the attachment in Base64.
    #
    # You can also specify overrides if you want by passing a hash instead of a string:
    #
    #  mail.attachments['filename.jpg'] = {mime_type: 'application/gzip',
    #                                      content: File.read('/path/to/filename.jpg')}
    #
    # If you want to use encoding other than Base64 then you will need to pass encoding
    # type along with the pre-encoded content as Mail doesn't know how to decode the
    # data:
    #
    #  file_content = SpecialEncode(File.read('/path/to/filename.jpg'))
    #  mail.attachments['filename.jpg'] = {mime_type: 'application/gzip',
    #                                      encoding: 'SpecialEncoding',
    #                                      content: file_content }
    #
    # You can also search for specific attachments:
    #
    #  # By Filename
    #  mail.attachments['filename.jpg']   # => Mail::Part object or nil
    #
    #  # or by index
    #  mail.attachments[0]                # => Mail::Part (first attachment)
    #
    def attachments
      if @_mail_was_called
        LateAttachmentsProxy.new(@_message.attachments)
      else
        @_message.attachments
      end
    end

    class LateAttachmentsProxy < SimpleDelegator
      def inline; self end
      def []=(_name, _content); _raise_error end

      private
        def _raise_error
          raise RuntimeError, "Can't add attachments after `mail` was called.\n" \
                              "Make sure to use `attachments[]=` before calling `mail`."
        end
    end

    # The main method that creates the message and renders the email templates. There are
    # two ways to call this method, with a block, or without a block.
    #
    # It accepts a headers hash. This hash allows you to specify
    # the most used headers in an email message, these are:
    #
    # * +:subject+ - The subject of the message, if this is omitted, Action Mailer will
    #   ask the \Rails I18n class for a translated +:subject+ in the scope of
    #   <tt>[mailer_scope, action_name]</tt> or if this is missing, will translate the
    #   humanized version of the +action_name+
    # * +:to+ - Who the message is destined for, can be a string of addresses, or an array
    #   of addresses.
    # * +:from+ - Who the message is from
    # * +:cc+ - Who you would like to Carbon-Copy on this email, can be a string of addresses,
    #   or an array of addresses.
    # * +:bcc+ - Who you would like to Blind-Carbon-Copy on this email, can be a string of
    #   addresses, or an array of addresses.
    # * +:reply_to+ - Who to set the +Reply-To+ header of the email to.
    # * +:date+ - The date to say the email was sent on.
    #
    # You can set default values for any of the above headers (except +:date+)
    # by using the ::default class method:
    #
    #  class Notifier < ActionMailer::Base
    #    default from: 'no-reply@test.lindsaar.net',
    #            bcc: 'email_logger@test.lindsaar.net',
    #            reply_to: 'bounces@test.lindsaar.net'
    #  end
    #
    # If you need other headers not listed above, you can either pass them in
    # as part of the headers hash or use the <tt>headers['name'] = value</tt>
    # method.
    #
    # When a +:return_path+ is specified as header, that value will be used as
    # the 'envelope from' address for the Mail message. Setting this is useful
    # when you want delivery notifications sent to a different address than the
    # one in +:from+. Mail will actually use the +:return_path+ in preference
    # to the +:sender+ in preference to the +:from+ field for the 'envelope
    # from' value.
    #
    # If you do not pass a block to the +mail+ method, it will find all
    # templates in the view paths using by default the mailer name and the
    # method name that it is being called from, it will then create parts for
    # each of these templates intelligently, making educated guesses on correct
    # content type and sequence, and return a fully prepared +Mail::Message+
    # ready to call <tt>:deliver</tt> on to send.
    #
    # For example:
    #
    #   class Notifier < ActionMailer::Base
    #     default from: 'no-reply@test.lindsaar.net'
    #
    #     def welcome
    #       mail(to: 'mikel@test.lindsaar.net')
    #     end
    #   end
    #
    # Will look for all templates at "app/views/notifier" with name "welcome".
    # If no welcome template exists, it will raise an ActionView::MissingTemplate error.
    #
    # However, those can be customized:
    #
    #   mail(template_path: 'notifications', template_name: 'another')
    #
    # And now it will look for all templates at "app/views/notifications" with name "another".
    #
    # If you do pass a block, you can render specific templates of your choice:
    #
    #   mail(to: 'mikel@test.lindsaar.net') do |format|
    #     format.text
    #     format.html
    #   end
    #
    # You can even render plain text directly without using a template:
    #
    #   mail(to: 'mikel@test.lindsaar.net') do |format|
    #     format.text { render plain: "Hello Mikel!" }
    #     format.html { render html: "<h1>Hello Mikel!</h1>".html_safe }
    #   end
    #
    # Which will render a +multipart/alternative+ email with +text/plain+ and
    # +text/html+ parts.
    #
    # The block syntax also allows you to customize the part headers if desired:
    #
    #   mail(to: 'mikel@test.lindsaar.net') do |format|
    #     format.text(content_transfer_encoding: "base64")
    #     format.html
    #   end
    #
    def mail(headers = {}, &block)
      return message if @_mail_was_called && headers.blank? && !block

      # At the beginning, do not consider class default for content_type
      content_type = headers[:content_type]

      headers = apply_defaults(headers)

      # Apply charset at the beginning so all fields are properly quoted
      message.charset = charset = headers[:charset]

      # Set configure delivery behavior
      wrap_delivery_behavior!(headers[:delivery_method], headers[:delivery_method_options])

      assign_headers_to_message(message, headers)

      # Render the templates and blocks
      responses = collect_responses(headers, &block)
      @_mail_was_called = true

      create_parts_from_responses(message, responses)
      wrap_inline_attachments(message)

      # Set up content type, reapply charset and handle parts order
      message.content_type = set_content_type(message, content_type, headers[:content_type])
      message.charset      = charset

      if message.multipart?
        message.body.set_sort_order(headers[:parts_order])
        message.body.sort_parts!
      end

      message
    end

    private
      # Used by #mail to set the content type of the message.
      #
      # It will use the given +user_content_type+, or multipart if the mail
      # message has any attachments. If the attachments are inline, the content
      # type will be "multipart/related", otherwise "multipart/mixed".
      #
      # If there is no content type passed in via headers, and there are no
      # attachments, or the message is multipart, then the default content type is
      # used.
      def set_content_type(m, user_content_type, class_default) # :doc:
        params = m.content_type_parameters || {}
        case
        when user_content_type.present?
          user_content_type
        when m.has_attachments?
          if m.attachments.all?(&:inline?)
            ["multipart", "related", params]
          else
            ["multipart", "mixed", params]
          end
        when m.multipart?
          ["multipart", "alternative", params]
        else
          m.content_type || class_default
        end
      end

      # Translates the +subject+ using \Rails I18n class under <tt>[mailer_scope, action_name]</tt> scope.
      # If it does not find a translation for the +subject+ under the specified scope it will default to a
      # humanized version of the <tt>action_name</tt>.
      # If the subject has interpolations, you can pass them through the +interpolations+ parameter.
      def default_i18n_subject(interpolations = {}) # :doc:
        mailer_scope = self.class.mailer_name.tr("/", ".")
        I18n.t(:subject, **interpolations.merge(scope: [mailer_scope, action_name], default: action_name.humanize))
      end

      # Emails do not support relative path links.
      def self.supports_path? # :doc:
        false
      end

      def apply_defaults(headers)
        default_values = self.class.default.except(*headers.keys).transform_values do |value|
          compute_default(value)
        end

        headers_with_defaults = headers.reverse_merge(default_values)
        headers_with_defaults[:subject] ||= default_i18n_subject
        headers_with_defaults
      end

      def compute_default(value)
        return value unless value.is_a?(Proc)

        if value.arity == 1
          instance_exec(self, &value)
        else
          instance_exec(&value)
        end
      end

      def assign_headers_to_message(message, headers)
        assignable = headers.except(:parts_order, :content_type, :body, :template_name,
                                    :template_path, :delivery_method, :delivery_method_options)
        assignable.each { |k, v| message[k] = v }
      end

      def collect_responses(headers, &block)
        if block_given?
          collect_responses_from_block(headers, &block)
        elsif headers[:body]
          collect_responses_from_text(headers)
        else
          collect_responses_from_templates(headers)
        end
      end

      def collect_responses_from_block(headers)
        templates_name = headers[:template_name] || action_name
        collector = ActionMailer::Collector.new(lookup_context) { render(templates_name) }
        yield(collector)
        collector.responses
      end

      def collect_responses_from_text(headers)
        [{
          body: headers.delete(:body),
          content_type: headers[:content_type] || "text/plain"
        }]
      end

      def collect_responses_from_templates(headers)
        templates_path = headers[:template_path] || self.class.mailer_name
        templates_name = headers[:template_name] || action_name

        each_template(Array(templates_path), templates_name).map do |template|
          format = template.format || self.formats.first
          {
            body: render(template: template, formats: [format]),
            content_type: Mime[format].to_s
          }
        end
      end

      def each_template(paths, name, &block)
        templates = lookup_context.find_all(name, paths)
        if templates.empty?
          raise ActionView::MissingTemplate.new(paths, name, paths, false, "mailer")
        else
          templates.uniq(&:format).each(&block)
        end
      end

      def wrap_inline_attachments(message)
        # If we have both types of attachment, wrap all the inline attachments
        # in multipart/related, but not the actual attachments
        if message.attachments.detect(&:inline?) && message.attachments.detect { |a| !a.inline? }
          related = Mail::Part.new
          related.content_type = "multipart/related"
          mixed = [ related ]

          message.parts.each do |p|
            if p.attachment? && !p.inline?
              mixed << p
            else
              related.add_part(p)
            end
          end

          message.parts.clear
          mixed.each { |c| message.add_part(c) }
        end
      end

      def create_parts_from_responses(m, responses)
        if responses.size == 1 && !m.has_attachments?
          responses[0].each { |k, v| m[k] = v }
        elsif responses.size > 1 && m.has_attachments?
          container = Mail::Part.new
          container.content_type = "multipart/alternative"
          responses.each { |r| insert_part(container, r, m.charset) }
          m.add_part(container)
        else
          responses.each { |r| insert_part(m, r, m.charset) }
        end
      end

      def insert_part(container, response, charset)
        response[:charset] ||= charset
        part = Mail::Part.new(response)
        container.add_part(part)
      end

      # This and #instrument_name is for caching instrument
      def instrument_payload(key)
        {
          mailer: mailer_name,
          key: key
        }
      end

      def instrument_name
        "action_mailer"
      end

      def _protected_ivars
        PROTECTED_IVARS
      end

      ActiveSupport.run_load_hooks(:action_mailer, self)
  end
end


// --- rb_url_helper.rb ---
# frozen_string_literal: true

require "active_support/core_ext/array/access"
require "active_support/core_ext/hash/keys"
require "active_support/core_ext/string/output_safety"
require "action_view/helpers/content_exfiltration_prevention_helper"
require "action_view/helpers/tag_helper"

module ActionView
  module Helpers # :nodoc:
    # = Action View URL \Helpers
    #
    # Provides a set of methods for making links and getting URLs that
    # depend on the routing subsystem (see ActionDispatch::Routing).
    # This allows you to use the same format for links in views
    # and controllers.
    module UrlHelper
      # This helper may be included in any class that includes the
      # URL helpers of a routes (routes.url_helpers). Some methods
      # provided here will only work in the context of a request
      # (link_to_unless_current, for instance), which must be provided
      # as a method called #request on the context.
      BUTTON_TAG_METHOD_VERBS = %w{patch put delete}
      extend ActiveSupport::Concern

      include TagHelper
      include ContentExfiltrationPreventionHelper

      module ClassMethods
        def _url_for_modules
          ActionView::RoutingUrlFor
        end
      end

      mattr_accessor :button_to_generates_button_tag, default: false

      # Basic implementation of url_for to allow use helpers without routes existence
      def url_for(options = nil) # :nodoc:
        case options
        when String
          options
        when :back
          _back_url
        else
          raise ArgumentError, "arguments passed to url_for can't be handled. Please require " \
                               "routes or provide your own implementation"
        end
      end

      def _back_url # :nodoc:
        _filtered_referrer || "javascript:history.back()"
      end
      private :_back_url

      def _filtered_referrer # :nodoc:
        if controller.respond_to?(:request)
          referrer = controller.request.env["HTTP_REFERER"]
          if referrer && URI(referrer).scheme != "javascript"
            referrer
          end
        end
      rescue URI::InvalidURIError
      end
      private :_filtered_referrer

      # Creates an anchor element of the given +name+ using a URL created by the set of +options+.
      # See the valid options in the documentation for +url_for+. It's also possible to
      # pass a \String instead of an options hash, which generates an anchor element that uses the
      # value of the \String as the href for the link. Using a <tt>:back</tt> \Symbol instead
      # of an options hash will generate a link to the referrer (a JavaScript back link
      # will be used in place of a referrer if none exists). If +nil+ is passed as the name
      # the value of the link itself will become the name.
      #
      # ==== Signatures
      #
      #   link_to(body, url, html_options = {})
      #     # url is a String; you can use URL helpers like
      #     # posts_path
      #
      #   link_to(body, url_options = {}, html_options = {})
      #     # url_options, except :method, is passed to url_for
      #
      #   link_to(options = {}, html_options = {}) do
      #     # name
      #   end
      #
      #   link_to(url, html_options = {}) do
      #     # name
      #   end
      #
      #   link_to(active_record_model)
      #
      # ==== Options
      # * <tt>:data</tt> - This option can be used to add custom data attributes.
      #
      # ==== Examples
      #
      # Because it relies on +url_for+, +link_to+ supports both older-style controller/action/id arguments
      # and newer RESTful routes. Current \Rails style favors RESTful routes whenever possible, so base
      # your application on resources and use
      #
      #   link_to "Profile", profile_path(@profile)
      #   # => <a href="/profiles/1">Profile</a>
      #
      # or the even pithier
      #
      #   link_to "Profile", @profile
      #   # => <a href="/profiles/1">Profile</a>
      #
      # in place of the older more verbose, non-resource-oriented
      #
      #   link_to "Profile", controller: "profiles", action: "show", id: @profile
      #   # => <a href="/profiles/show/1">Profile</a>
      #
      # Similarly,
      #
      #   link_to "Profiles", profiles_path
      #   # => <a href="/profiles">Profiles</a>
      #
      # is better than
      #
      #   link_to "Profiles", controller: "profiles"
      #   # => <a href="/profiles">Profiles</a>
      #
      # When name is +nil+ the href is presented instead
      #
      #   link_to nil, "http://example.com"
      #   # => <a href="http://www.example.com">http://www.example.com</a>
      #
      # More concise yet, when +name+ is an Active Record model that defines a
      # +to_s+ method returning a default value or a model instance attribute
      #
      #   link_to @profile
      #   # => <a href="http://www.example.com/profiles/1">Eileen</a>
      #
      # You can use a block as well if your link target is hard to fit into the name parameter. ERB example:
      #
      #   <%= link_to(@profile) do %>
      #     <strong><%= @profile.name %></strong> -- <span>Check it out!</span>
      #   <% end %>
      #   # => <a href="/profiles/1">
      #          <strong>David</strong> -- <span>Check it out!</span>
      #        </a>
      #
      # Classes and ids for CSS are easy to produce:
      #
      #   link_to "Articles", articles_path, id: "news", class: "article"
      #   # => <a href="/articles" class="article" id="news">Articles</a>
      #
      # Be careful when using the older argument style, as an extra literal hash is needed:
      #
      #   link_to "Articles", { controller: "articles" }, id: "news", class: "article"
      #   # => <a href="/articles" class="article" id="news">Articles</a>
      #
      # Leaving the hash off gives the wrong link:
      #
      #   link_to "WRONG!", controller: "articles", id: "news", class: "article"
      #   # => <a href="/articles/index/news?class=article">WRONG!</a>
      #
      # +link_to+ can also produce links with anchors or query strings:
      #
      #   link_to "Comment wall", profile_path(@profile, anchor: "wall")
      #   # => <a href="/profiles/1#wall">Comment wall</a>
      #
      #   link_to "Ruby on Rails search", controller: "searches", query: "ruby on rails"
      #   # => <a href="/searches?query=ruby+on+rails">Ruby on Rails search</a>
      #
      #   link_to "Nonsense search", searches_path(foo: "bar", baz: "quux")
      #   # => <a href="/searches?foo=bar&baz=quux">Nonsense search</a>
      #
      # You can set any link attributes such as <tt>target</tt>, <tt>rel</tt>, <tt>type</tt>:
      #
      #   link_to "External link", "http://www.rubyonrails.org/", target: "_blank", rel: "nofollow"
      #   # => <a href="http://www.rubyonrails.org/" target="_blank" rel="nofollow">External link</a>
      #
      # ==== Turbo
      #
      # Rails 7 ships with Turbo enabled by default. Turbo provides the following +:data+ options:
      #
      # * <tt>turbo_method: symbol of HTTP verb</tt> - Performs a Turbo link visit
      #   with the given HTTP verb. Forms are recommended when performing non-+GET+ requests.
      #   Only use <tt>data-turbo-method</tt> where a form is not possible.
      #
      # * <tt>turbo_confirm: "question?"</tt> - Adds a confirmation dialog to the link with the
      #   given value.
      #
      # {Consult the Turbo Handbook for more information on the options
      # above.}[https://turbo.hotwired.dev/handbook/drive#performing-visits-with-a-different-method]
      #
      # ===== \Examples
      #
      #   link_to "Delete profile", @profile, data: { turbo_method: :delete }
      #   # => <a href="/profiles/1" data-turbo-method="delete">Delete profile</a>
      #
      #   link_to "Visit Other Site", "https://rubyonrails.org/", data: { turbo_confirm: "Are you sure?" }
      #   # => <a href="https://rubyonrails.org/" data-turbo-confirm="Are you sure?">Visit Other Site</a>
      #
      # ==== Deprecated: \Rails UJS Attributes
      #
      # Prior to \Rails 7, \Rails shipped with a JavaScript library called <tt>@rails/ujs</tt> on by default. Following \Rails 7,
      # this library is no longer on by default. This library integrated with the following options:
      #
      # * <tt>method: symbol of HTTP verb</tt> - This modifier will dynamically
      #   create an HTML form and immediately submit the form for processing using
      #   the HTTP verb specified. Useful for having links perform a POST operation
      #   in dangerous actions like deleting a record (which search bots can follow
      #   while spidering your site). Supported verbs are <tt>:post</tt>, <tt>:delete</tt>, <tt>:patch</tt>, and <tt>:put</tt>.
      #   Note that if the user has JavaScript disabled, the request will fall back
      #   to using GET. If <tt>href: '#'</tt> is used and the user has JavaScript
      #   disabled clicking the link will have no effect. If you are relying on the
      #   POST behavior, you should check for it in your controller's action by using
      #   the request object's methods for <tt>post?</tt>, <tt>delete?</tt>, <tt>patch?</tt>, or <tt>put?</tt>.
      # * <tt>remote: true</tt> - This will allow <tt>@rails/ujs</tt>
      #   to make an Ajax request to the URL in question instead of following
      #   the link.
      #
      # <tt>@rails/ujs</tt> also integrated with the following +:data+ options:
      #
      # * <tt>confirm: "question?"</tt> - This will allow <tt>@rails/ujs</tt>
      #   to prompt with the question specified (in this case, the
      #   resulting text would be <tt>question?</tt>). If the user accepts, the
      #   link is processed normally, otherwise no action is taken.
      # * <tt>:disable_with</tt> - Value of this parameter will be used as the
      #   name for a disabled version of the link.
      #
      # ===== \Rails UJS Examples
      #
      #   link_to "Remove Profile", profile_path(@profile), method: :delete
      #   # => <a href="/profiles/1" rel="nofollow" data-method="delete">Remove Profile</a>
      #
      #   link_to "Visit Other Site", "http://www.rubyonrails.org/", data: { confirm: "Are you sure?" }
      #   # => <a href="http://www.rubyonrails.org/" data-confirm="Are you sure?">Visit Other Site</a>
      #
      def link_to(name = nil, options = nil, html_options = nil, &block)
        html_options, options, name = options, name, block if block_given?
        options ||= {}

        html_options = convert_options_to_data_attributes(options, html_options)

        url = url_target(name, options)
        html_options["href"] ||= url

        content_tag("a", name || url, html_options, &block)
      end

      # Generates a form containing a single button that submits to the URL created
      # by the set of +options+. This is the safest method to ensure links that
      # cause changes to your data are not triggered by search bots or accelerators.
      #
      # You can control the form and button behavior with +html_options+. Most
      # values in +html_options+ are passed through to the button element. For
      # example, passing a +:class+ option within +html_options+ will set the
      # class attribute of the button element.
      #
      # The class attribute of the form element can be set by passing a
      # +:form_class+ option within +html_options+. It defaults to
      # <tt>"button_to"</tt> to allow styling of the form and its children.
      #
      # The form submits a POST request by default. You can specify a different
      # HTTP verb via the +:method+ option within +html_options+.
      #
      # If the HTML button generated from +button_to+ does not work with your layout, you can
      # consider using the +link_to+ method with the +data-turbo-method+
      # attribute as described in the +link_to+ documentation.
      #
      # ==== Options
      # The +options+ hash accepts the same options as +url_for+. To generate a
      # <tt><form></tt> element without an <tt>[action]</tt> attribute, pass
      # <tt>false</tt>:
      #
      #   <%= button_to "New", false %>
      #   # => "<form method="post" class="button_to">
      #   #      <button type="submit">New</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"/>
      #   #    </form>"
      #
      # Most values in +html_options+ are passed through to the button element,
      # but there are a few special options:
      #
      # * <tt>:method</tt> - \Symbol of HTTP verb. Supported verbs are <tt>:post</tt>, <tt>:get</tt>,
      #   <tt>:delete</tt>, <tt>:patch</tt>, and <tt>:put</tt>. By default it will be <tt>:post</tt>.
      # * <tt>:disabled</tt> - If set to true, it will generate a disabled button.
      # * <tt>:data</tt> - This option can be used to add custom data attributes.
      # * <tt>:form</tt> - This hash will be form attributes
      # * <tt>:form_class</tt> - This controls the class of the form within which the submit button will
      #   be placed
      # * <tt>:params</tt> - \Hash of parameters to be rendered as hidden fields within the form.
      #
      # ==== Examples
      #   <%= button_to "New", action: "new" %>
      #   # => "<form method="post" action="/controller/new" class="button_to">
      #   #      <button type="submit">New</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6" autocomplete="off"/>
      #   #    </form>"
      #
      #   <%= button_to "New", new_article_path %>
      #   # => "<form method="post" action="/articles/new" class="button_to">
      #   #      <button type="submit">New</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6" autocomplete="off"/>
      #   #    </form>"
      #
      #   <%= button_to "New", new_article_path, params: { time: Time.now  } %>
      #   # => "<form method="post" action="/articles/new" class="button_to">
      #   #      <button type="submit">New</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"/>
      #   #      <input type="hidden" name="time" value="2021-04-08 14:06:09 -0500" autocomplete="off">
      #   #    </form>"
      #
      #   <%= button_to [:make_happy, @user] do %>
      #     Make happy <strong><%= @user.name %></strong>
      #   <% end %>
      #   # => "<form method="post" action="/users/1/make_happy" class="button_to">
      #   #      <button type="submit">
      #   #        Make happy <strong><%= @user.name %></strong>
      #   #      </button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"  autocomplete="off"/>
      #   #    </form>"
      #
      #   <%= button_to "New", { action: "new" }, form_class: "new-thing" %>
      #   # => "<form method="post" action="/controller/new" class="new-thing">
      #   #      <button type="submit">New</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"  autocomplete="off"/>
      #   #    </form>"
      #
      #   <%= button_to "Create", { action: "create" }, form: { "data-type" => "json" } %>
      #   # => "<form method="post" action="/images/create" class="button_to" data-type="json">
      #   #      <button type="submit">Create</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"  autocomplete="off"/>
      #   #    </form>"
      #
      # ==== Deprecated: \Rails UJS Attributes
      #
      # Prior to \Rails 7, \Rails shipped with a JavaScript library called <tt>@rails/ujs</tt> on by default. Following \Rails 7,
      # this library is no longer on by default. This library integrated with the following options:
      #
      # * <tt>:remote</tt> -  If set to true, will allow <tt>@rails/ujs</tt> to control the
      #   submit behavior. By default this behavior is an Ajax submit.
      #
      # <tt>@rails/ujs</tt> also integrated with the following +:data+ options:
      #
      # * <tt>confirm: "question?"</tt> - This will allow <tt>@rails/ujs</tt>
      #   to prompt with the question specified (in this case, the
      #   resulting text would be <tt>question?</tt>). If the user accepts, the
      #   button is processed normally, otherwise no action is taken.
      # * <tt>:disable_with</tt> - Value of this parameter will be
      #   used as the value for a disabled version of the submit
      #   button when the form is submitted.
      #
      # ===== \Rails UJS Examples
      #
      #   <%= button_to "Create", { action: "create" }, remote: true, form: { "data-type" => "json" } %>
      #   # => "<form method="post" action="/images/create" class="button_to" data-remote="true" data-type="json">
      #   #      <button type="submit">Create</button>
      #   #      <input name="authenticity_token" type="hidden" value="10f2163b45388899ad4d5ae948988266befcb6c3d1b2451cf657a0c293d605a6"  autocomplete="off"/>
      #   #    </form>"
      #
      def button_to(name = nil, options = nil, html_options = nil, &block)
        html_options, options = options, name if block_given?
        html_options ||= {}
        html_options = html_options.stringify_keys

        url =
          case options
          when FalseClass then nil
          else url_for(options)
          end

        remote = html_options.delete("remote")
        params = html_options.delete("params")

        authenticity_token = html_options.delete("authenticity_token")

        method     = (html_options.delete("method").presence || method_for_options(options)).to_s
        method_tag = BUTTON_TAG_METHOD_VERBS.include?(method) ? method_tag(method) : "".html_safe

        form_method  = method == "get" ? "get" : "post"
        form_options = html_options.delete("form") || {}
        form_options[:class] ||= html_options.delete("form_class") || "button_to"
        form_options[:method] = form_method
        form_options[:action] = url
        form_options[:'data-remote'] = true if remote

        request_token_tag = if form_method == "post"
          request_method = method.empty? ? "post" : method
          token_tag(authenticity_token, form_options: { action: url, method: request_method })
        else
          ""
        end

        html_options = convert_options_to_data_attributes(options, html_options)
        html_options["type"] = "submit"

        button = if block_given?
          content_tag("button", html_options, &block)
        elsif button_to_generates_button_tag
          content_tag("button", name || url, html_options, &block)
        else
          html_options["value"] = name || url
          tag("input", html_options)
        end

        inner_tags = method_tag.safe_concat(button).safe_concat(request_token_tag)
        if params
          to_form_params(params).each do |param|
            inner_tags.safe_concat tag(:input, type: "hidden", name: param[:name], value: param[:value],
                                       autocomplete: "off")
          end
        end
        html = content_tag("form", inner_tags, form_options)
        prevent_content_exfiltration(html)
      end

      # Creates a link tag of the given +name+ using a URL created by the set of
      # +options+ unless the current request URI is the same as the links, in
      # which case only the name is returned (or the given block is yielded, if
      # one exists). You can give +link_to_unless_current+ a block which will
      # specialize the default behavior (e.g., show a "Start Here" link rather
      # than the link's text).
      #
      # ==== Examples
      # Let's say you have a navigation menu...
      #
      #   <ul id="navbar">
      #     <li><%= link_to_unless_current("Home", { action: "index" }) %></li>
      #     <li><%= link_to_unless_current("About Us", { action: "about" }) %></li>
      #   </ul>
      #
      # If in the "about" action, it will render...
      #
      #   <ul id="navbar">
      #     <li><a href="/controller/index">Home</a></li>
      #     <li>About Us</li>
      #   </ul>
      #
      # ...but if in the "index" action, it will render:
      #
      #   <ul id="navbar">
      #     <li>Home</li>
      #     <li><a href="/controller/about">About Us</a></li>
      #   </ul>
      #
      # The implicit block given to +link_to_unless_current+ is evaluated if the current
      # action is the action given. So, if we had a comments page and wanted to render a
      # "Go Back" link instead of a link to the comments page, we could do something like this...
      #
      #    <%=
      #        link_to_unless_current("Comment", { controller: "comments", action: "new" }) do
      #           link_to("Go back", { controller: "posts", action: "index" })
      #        end
      #     %>
      def link_to_unless_current(name, options = {}, html_options = {}, &block)
        link_to_unless current_page?(options), name, options, html_options, &block
      end

      # Creates a link tag of the given +name+ using a URL created by the set of
      # +options+ unless +condition+ is true, in which case only the name is
      # returned. To specialize the default behavior (i.e., show a login link rather
      # than just the plaintext link text), you can pass a block that
      # accepts the name or the full argument list for +link_to_unless+.
      #
      # ==== Examples
      #   <%= link_to_unless(@current_user.nil?, "Reply", { action: "reply" }) %>
      #   # If the user is logged in...
      #   # => <a href="/controller/reply/">Reply</a>
      #
      #   <%=
      #      link_to_unless(@current_user.nil?, "Reply", { action: "reply" }) do |name|
      #        link_to(name, { controller: "accounts", action: "signup" })
      #      end
      #   %>
      #   # If the user is logged in...
      #   # => <a href="/controller/reply/">Reply</a>
      #   # If not...
      #   # => <a href="/accounts/signup">Reply</a>
      def link_to_unless(condition, name, options = {}, html_options = {}, &block)
        link_to_if !condition, name, options, html_options, &block
      end

      # Creates a link tag of the given +name+ using a URL created by the set of
      # +options+ if +condition+ is true, otherwise only the name is
      # returned. To specialize the default behavior, you can pass a block that
      # accepts the name or the full argument list for +link_to_if+.
      #
      # ==== Examples
      #   <%= link_to_if(@current_user.nil?, "Login", { controller: "sessions", action: "new" }) %>
      #   # If the user isn't logged in...
      #   # => <a href="/sessions/new/">Login</a>
      #
      #   <%=
      #      link_to_if(@current_user.nil?, "Login", { controller: "sessions", action: "new" }) do
      #        link_to(@current_user.login, { controller: "accounts", action: "show", id: @current_user })
      #      end
      #   %>
      #   # If the user isn't logged in...
      #   # => <a href="/sessions/new/">Login</a>
      #   # If they are logged in...
      #   # => <a href="/accounts/show/3">my_username</a>
      def link_to_if(condition, name, options = {}, html_options = {}, &block)
        if condition
          link_to(name, options, html_options)
        else
          if block_given?
            block.arity <= 1 ? capture(name, &block) : capture(name, options, html_options, &block)
          else
            ERB::Util.html_escape(name)
          end
        end
      end

      # Creates a mailto link tag to the specified +email_address+, which is
      # also used as the name of the link unless +name+ is specified. Additional
      # HTML attributes for the link can be passed in +html_options+.
      #
      # +mail_to+ has several methods for customizing the email itself by
      # passing special keys to +html_options+.
      #
      # ==== Options
      # * <tt>:subject</tt> - Preset the subject line of the email.
      # * <tt>:body</tt> - Preset the body of the email.
      # * <tt>:cc</tt> - Carbon Copy additional recipients on the email.
      # * <tt>:bcc</tt> - Blind Carbon Copy additional recipients on the email.
      # * <tt>:reply_to</tt> - Preset the +Reply-To+ field of the email.
      #
      # ==== Obfuscation
      # Prior to \Rails 4.0, +mail_to+ provided options for encoding the address
      # in order to hinder email harvesters.  To take advantage of these options,
      # install the +actionview-encoded_mail_to+ gem.
      #
      # ==== Examples
      #   mail_to "me@domain.com"
      #   # => <a href="mailto:me@domain.com">me@domain.com</a>
      #
      #   mail_to "me@domain.com", "My email"
      #   # => <a href="mailto:me@domain.com">My email</a>
      #
      #   mail_to "me@domain.com", cc: "ccaddress@domain.com",
      #            subject: "This is an example email"
      #   # => <a href="mailto:me@domain.com?cc=ccaddress@domain.com&subject=This%20is%20an%20example%20email">me@domain.com</a>
      #
      # You can use a block as well if your link target is hard to fit into the name parameter. ERB example:
      #
      #   <%= mail_to "me@domain.com" do %>
      #     <strong>Email me:</strong> <span>me@domain.com</span>
      #   <% end %>
      #   # => <a href="mailto:me@domain.com">
      #          <strong>Email me:</strong> <span>me@domain.com</span>
      #        </a>
      def mail_to(email_address, name = nil, html_options = {}, &block)
        html_options, name = name, nil if name.is_a?(Hash)
        html_options = (html_options || {}).stringify_keys

        extras = %w{ cc bcc body subject reply_to }.map! { |item|
          option = html_options.delete(item).presence || next
          "#{item.dasherize}=#{ERB::Util.url_encode(option)}"
        }.compact
        extras = extras.empty? ? "" : "?" + extras.join("&")

        encoded_email_address = ERB::Util.url_encode(email_address).gsub("%40", "@")
        html_options["href"] = "mailto:#{encoded_email_address}#{extras}"

        content_tag("a", name || email_address, html_options, &block)
      end

      # True if the current request URI was generated by the given +options+.
      #
      # ==== Examples
      # Let's say we're in the <tt>http://www.example.com/shop/checkout?order=desc&page=1</tt> action.
      #
      #   current_page?(action: 'process')
      #   # => false
      #
      #   current_page?(action: 'checkout')
      #   # => true
      #
      #   current_page?(controller: 'library', action: 'checkout')
      #   # => false
      #
      #   current_page?(controller: 'shop', action: 'checkout')
      #   # => true
      #
      #   current_page?(controller: 'shop', action: 'checkout', order: 'asc')
      #   # => false
      #
      #   current_page?(controller: 'shop', action: 'checkout', order: 'desc', page: '1')
      #   # => true
      #
      #   current_page?(controller: 'shop', action: 'checkout', order: 'desc', page: '2')
      #   # => false
      #
      #   current_page?('http://www.example.com/shop/checkout')
      #   # => true
      #
      #   current_page?('http://www.example.com/shop/checkout', check_parameters: true)
      #   # => false
      #
      #   current_page?('/shop/checkout')
      #   # => true
      #
      #   current_page?('http://www.example.com/shop/checkout?order=desc&page=1')
      #   # => true
      #
      # Let's say we're in the <tt>http://www.example.com/products</tt> action with method POST in case of invalid product.
      #
      #   current_page?(controller: 'product', action: 'index')
      #   # => false
      #
      # We can also pass in the symbol arguments instead of strings.
      #
      def current_page?(options = nil, check_parameters: false, **options_as_kwargs)
        unless request
          raise "You cannot use helpers that need to determine the current " \
                "page unless your view context provides a Request object " \
                "in a #request method"
        end

        return false unless request.get? || request.head?

        options ||= options_as_kwargs
        check_parameters ||= options.is_a?(Hash) && options.delete(:check_parameters)
        url_string = URI::DEFAULT_PARSER.unescape(url_for(options)).force_encoding(Encoding::BINARY)

        # We ignore any extra parameters in the request_uri if the
        # submitted URL doesn't have any either. This lets the function
        # work with things like ?order=asc
        # the behavior can be disabled with check_parameters: true
        request_uri = url_string.index("?") || check_parameters ? request.fullpath : request.path
        request_uri = URI::DEFAULT_PARSER.unescape(request_uri).force_encoding(Encoding::BINARY)

        if %r{^\w+://}.match?(url_string)
          request_uri = +"#{request.protocol}#{request.host_with_port}#{request_uri}"
        end

        remove_trailing_slash!(url_string)
        remove_trailing_slash!(request_uri)

        url_string == request_uri
      end

      if RUBY_VERSION.start_with?("2.7")
        using Module.new {
          refine UrlHelper do
            alias :_current_page? :current_page?
          end
        }

        def current_page?(*args) # :nodoc:
          options = args.pop
          options.is_a?(Hash) ? _current_page?(*args, **options) : _current_page?(*args, options)
        end
      end

      # Creates an SMS anchor link tag to the specified +phone_number+. When the
      # link is clicked, the default SMS messaging app is opened ready to send a
      # message to the linked phone number. If the +body+ option is specified,
      # the contents of the message will be preset to +body+.
      #
      # If +name+ is not specified, +phone_number+ will be used as the name of
      # the link.
      #
      # A +country_code+ option is supported, which prepends a plus sign and the
      # given country code to the linked phone number. For example,
      # <tt>country_code: "01"</tt> will prepend <tt>+01</tt> to the linked
      # phone number.
      #
      # Additional HTML attributes for the link can be passed via +html_options+.
      #
      # ==== Options
      # * <tt>:country_code</tt> - Prepend the country code to the phone number.
      # * <tt>:body</tt> - Preset the body of the message.
      #
      # ==== Examples
      #   sms_to "5155555785"
      #   # => <a href="sms:5155555785;">5155555785</a>
      #
      #   sms_to "5155555785", country_code: "01"
      #   # => <a href="sms:+015155555785;">5155555785</a>
      #
      #   sms_to "5155555785", "Text me"
      #   # => <a href="sms:5155555785;">Text me</a>
      #
      #   sms_to "5155555785", body: "I have a question about your product."
      #   # => <a href="sms:5155555785;?body=I%20have%20a%20question%20about%20your%20product">5155555785</a>
      #
      # You can use a block as well if your link target is hard to fit into the name parameter. \ERB example:
      #
      #   <%= sms_to "5155555785" do %>
      #     <strong>Text me:</strong>
      #   <% end %>
      #   # => <a href="sms:5155555785;">
      #          <strong>Text me:</strong>
      #        </a>
      def sms_to(phone_number, name = nil, html_options = {}, &block)
        html_options, name = name, nil if name.is_a?(Hash)
        html_options = (html_options || {}).stringify_keys

        country_code = html_options.delete("country_code").presence
        country_code = country_code ? "+#{ERB::Util.url_encode(country_code)}" : ""

        body = html_options.delete("body").presence
        body = body ? "?&body=#{ERB::Util.url_encode(body)}" : ""

        encoded_phone_number = ERB::Util.url_encode(phone_number)
        html_options["href"] = "sms:#{country_code}#{encoded_phone_number};#{body}"

        content_tag("a", name || phone_number, html_options, &block)
      end

      # Creates a TEL anchor link tag to the specified +phone_number+. When the
      # link is clicked, the default app to make phone calls is opened and
      # prepopulated with the phone number.
      #
      # If +name+ is not specified, +phone_number+ will be used as the name of
      # the link.
      #
      # A +country_code+ option is supported, which prepends a plus sign and the
      # given country code to the linked phone number. For example,
      # <tt>country_code: "01"</tt> will prepend <tt>+01</tt> to the linked
      # phone number.
      #
      # Additional HTML attributes for the link can be passed via +html_options+.
      #
      # ==== Options
      # * <tt>:country_code</tt> - Prepends the country code to the phone number
      #
      # ==== Examples
      #   phone_to "1234567890"
      #   # => <a href="tel:1234567890">1234567890</a>
      #
      #   phone_to "1234567890", "Phone me"
      #   # => <a href="tel:1234567890">Phone me</a>
      #
      #   phone_to "1234567890", country_code: "01"
      #   # => <a href="tel:+011234567890">1234567890</a>
      #
      # You can use a block as well if your link target is hard to fit into the name parameter. \ERB example:
      #
      #   <%= phone_to "1234567890" do %>
      #     <strong>Phone me:</strong>
      #   <% end %>
      #   # => <a href="tel:1234567890">
      #          <strong>Phone me:</strong>
      #        </a>
      def phone_to(phone_number, name = nil, html_options = {}, &block)
        html_options, name = name, nil if name.is_a?(Hash)
        html_options = (html_options || {}).stringify_keys

        country_code = html_options.delete("country_code").presence
        country_code = country_code.nil? ? "" : "+#{ERB::Util.url_encode(country_code)}"

        encoded_phone_number = ERB::Util.url_encode(phone_number)
        html_options["href"] = "tel:#{country_code}#{encoded_phone_number}"

        content_tag("a", name || phone_number, html_options, &block)
      end

      private
        def convert_options_to_data_attributes(options, html_options)
          if html_options
            html_options = html_options.stringify_keys
            html_options["data-remote"] = "true" if link_to_remote_options?(options) || link_to_remote_options?(html_options)

            method = html_options.delete("method")

            add_method_to_attributes!(html_options, method) if method

            html_options
          else
            link_to_remote_options?(options) ? { "data-remote" => "true" } : {}
          end
        end

        def url_target(name, options)
          if name.respond_to?(:model_name) && options.is_a?(Hash) && options.empty?
            url_for(name)
          else
            url_for(options)
          end
        end

        def link_to_remote_options?(options)
          if options.is_a?(Hash)
            options.delete("remote") || options.delete(:remote)
          end
        end

        def add_method_to_attributes!(html_options, method)
          if method_not_get_method?(method) && !html_options["rel"]&.match?(/nofollow/)
            if html_options["rel"].blank?
              html_options["rel"] = "nofollow"
            else
              html_options["rel"] = "#{html_options["rel"]} nofollow"
            end
          end
          html_options["data-method"] = method
        end

        def method_for_options(options)
          if options.is_a?(Array)
            method_for_options(options.last)
          elsif options.respond_to?(:persisted?)
            options.persisted? ? :patch : :post
          elsif options.respond_to?(:to_model)
            method_for_options(options.to_model)
          end
        end

        STRINGIFIED_COMMON_METHODS = {
          get:    "get",
          delete: "delete",
          patch:  "patch",
          post:   "post",
          put:    "put",
        }.freeze

        def method_not_get_method?(method)
          return false unless method
          (STRINGIFIED_COMMON_METHODS[method] || method.to_s.downcase) != "get"
        end

        def token_tag(token = nil, form_options: {})
          if token != false && defined?(protect_against_forgery?) && protect_against_forgery?
            token =
              if token == true || token.nil?
                form_authenticity_token(form_options: form_options.merge(authenticity_token: token))
              else
                token
              end
            tag(:input, type: "hidden", name: request_forgery_protection_token.to_s, value: token, autocomplete: "off")
          else
            ""
          end
        end

        def method_tag(method)
          tag("input", type: "hidden", name: "_method", value: method.to_s, autocomplete: "off")
        end

        # Returns an array of hashes each containing :name and :value keys
        # suitable for use as the names and values of form input fields:
        #
        #   to_form_params(name: 'David', nationality: 'Danish')
        #   # => [{name: 'name', value: 'David'}, {name: 'nationality', value: 'Danish'}]
        #
        #   to_form_params(country: { name: 'Denmark' })
        #   # => [{name: 'country[name]', value: 'Denmark'}]
        #
        #   to_form_params(countries: ['Denmark', 'Sweden']})
        #   # => [{name: 'countries[]', value: 'Denmark'}, {name: 'countries[]', value: 'Sweden'}]
        #
        # An optional namespace can be passed to enclose key names:
        #
        #   to_form_params({ name: 'Denmark' }, 'country')
        #   # => [{name: 'country[name]', value: 'Denmark'}]
        def to_form_params(attribute, namespace = nil)
          attribute = if attribute.respond_to?(:permitted?)
            attribute.to_h
          else
            attribute
          end

          params = []
          case attribute
          when Hash
            attribute.each do |key, value|
              prefix = namespace ? "#{namespace}[#{key}]" : key
              params.push(*to_form_params(value, prefix))
            end
          when Array
            array_prefix = "#{namespace}[]"
            attribute.each do |value|
              params.push(*to_form_params(value, array_prefix))
            end
          else
            params << { name: namespace.to_s, value: attribute.to_param }
          end

          params.sort_by { |pair| pair[:name] }
        end

        def remove_trailing_slash!(url_string)
          trailing_index = (url_string.index("?") || 0) - 1
          url_string[trailing_index] = "" if url_string[trailing_index] == "/"
        end
    end
  end
end


// --- rb_form_tag_helper.rb ---
# frozen_string_literal: true

require "cgi"
require "action_view/helpers/content_exfiltration_prevention_helper"
require "action_view/helpers/url_helper"
require "action_view/helpers/text_helper"
require "active_support/core_ext/string/output_safety"
require "active_support/core_ext/module/attribute_accessors"

module ActionView
  module Helpers # :nodoc:
    # = Action View Form Tag \Helpers
    #
    # Provides a number of methods for creating form tags that don't rely on an Active Record object assigned to the template like
    # FormHelper does. Instead, you provide the names and values manually.
    #
    # NOTE: The HTML options <tt>disabled</tt>, <tt>readonly</tt>, and <tt>multiple</tt> can all be treated as booleans. So specifying
    # <tt>disabled: true</tt> will give <tt>disabled="disabled"</tt>.
    module FormTagHelper
      extend ActiveSupport::Concern

      include UrlHelper
      include TextHelper
      include ContentExfiltrationPreventionHelper

      mattr_accessor :embed_authenticity_token_in_remote_forms
      self.embed_authenticity_token_in_remote_forms = nil

      mattr_accessor :default_enforce_utf8, default: true

      # Starts a form tag that points the action to a URL configured with <tt>url_for_options</tt> just like
      # ActionController::Base#url_for. The method for the form defaults to POST.
      #
      # ==== Options
      # * <tt>:multipart</tt> - If set to true, the enctype is set to "multipart/form-data".
      # * <tt>:method</tt> - The method to use when submitting the form, usually either "get" or "post".
      #   If "patch", "put", "delete", or another verb is used, a hidden input with name <tt>_method</tt>
      #   is added to simulate the verb over post.
      # * <tt>:authenticity_token</tt> - Authenticity token to use in the form. Use only if you need to
      #   pass custom authenticity token string, or to not add authenticity_token field at all
      #   (by passing <tt>false</tt>).  Remote forms may omit the embedded authenticity token
      #   by setting <tt>config.action_view.embed_authenticity_token_in_remote_forms = false</tt>.
      #   This is helpful when you're fragment-caching the form. Remote forms get the
      #   authenticity token from the <tt>meta</tt> tag, so embedding is unnecessary unless you
      #   support browsers without JavaScript.
      # * <tt>:remote</tt> - If set to true, will allow the Unobtrusive JavaScript drivers to control the
      #   submit behavior. By default this behavior is an ajax submit.
      # * <tt>:enforce_utf8</tt> - If set to false, a hidden input with name utf8 is not output.
      # * Any other key creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   form_tag('/posts')
      #   # => <form action="/posts" method="post">
      #
      #   form_tag('/posts/1', method: :put)
      #   # => <form action="/posts/1" method="post"> ... <input name="_method" type="hidden" value="put" /> ...
      #
      #   form_tag('/upload', multipart: true)
      #   # => <form action="/upload" method="post" enctype="multipart/form-data">
      #
      #   <%= form_tag('/posts') do -%>
      #     <div><%= submit_tag 'Save' %></div>
      #   <% end -%>
      #   # => <form action="/posts" method="post"><div><input type="submit" name="commit" value="Save" /></div></form>
      #
      #   <%= form_tag('/posts', remote: true) %>
      #   # => <form action="/posts" method="post" data-remote="true">
      #
      #   form_tag(false, method: :get)
      #   # => <form method="get">
      #
      #   form_tag('http://far.away.com/form', authenticity_token: false)
      #   # form without authenticity token
      #
      #   form_tag('http://far.away.com/form', authenticity_token: "cf50faa3fe97702ca1ae")
      #   # form with custom authenticity token
      #
      def form_tag(url_for_options = {}, options = {}, &block)
        html_options = html_options_for_form(url_for_options, options)
        if block_given?
          form_tag_with_body(html_options, capture(&block))
        else
          form_tag_html(html_options)
        end
      end

      # Generate an HTML <tt>id</tt> attribute value for the given name and
      # field combination
      #
      # Return the value generated by the <tt>FormBuilder</tt> for the given
      # attribute name.
      #
      #   <%= label_tag :post, :title %>
      #   <%= text_field :post, :title, aria: { describedby: field_id(:post, :title, :error) } %>
      #   <%= tag.span("is blank", id: field_id(:post, :title, :error) %>
      #
      # In the example above, the <tt><input type="text"></tt> element built by
      # the call to <tt>text_field</tt> declares an
      # <tt>aria-describedby</tt> attribute referencing the <tt><span></tt>
      # element, sharing a common <tt>id</tt> root (<tt>post_title</tt>, in this
      # case).
      def field_id(object_name, method_name, *suffixes, index: nil, namespace: nil)
        if object_name.respond_to?(:model_name)
          object_name = object_name.model_name.singular
        end

        sanitized_object_name = object_name.to_s.gsub(/\]\[|[^-a-zA-Z0-9:.]/, "_").delete_suffix("_")

        sanitized_method_name = method_name.to_s.delete_suffix("?")

        [
          namespace,
          sanitized_object_name.presence,
          (index unless sanitized_object_name.empty?),
          sanitized_method_name,
          *suffixes,
        ].tap(&:compact!).join("_")
      end

      # Generate an HTML <tt>name</tt> attribute value for the given name and
      # field combination
      #
      # Return the value generated by the <tt>FormBuilder</tt> for the given
      # attribute name.
      #
      #   <%= text_field :post, :title, name: field_name(:post, :title, :subtitle) %>
      #   <%# => <input type="text" name="post[title][subtitle]"> %>
      #
      #   <%= text_field :post, :tag, name: field_name(:post, :tag, multiple: true) %>
      #   <%# => <input type="text" name="post[tag][]"> %>
      #
      def field_name(object_name, method_name, *method_names, multiple: false, index: nil)
        names = method_names.map! { |name| "[#{name}]" }.join

        # a little duplication to construct fewer strings
        case
        when object_name.blank?
          "#{method_name}#{names}#{multiple ? "[]" : ""}"
        when index
          "#{object_name}[#{index}][#{method_name}]#{names}#{multiple ? "[]" : ""}"
        else
          "#{object_name}[#{method_name}]#{names}#{multiple ? "[]" : ""}"
        end
      end

      # Creates a dropdown selection box, or if the <tt>:multiple</tt> option is set to true, a multiple
      # choice selection box.
      #
      # Helpers::FormOptions can be used to create common select boxes such as countries, time zones, or
      # associated records. <tt>option_tags</tt> is a string containing the option tags for the select box.
      #
      # ==== Options
      # * <tt>:multiple</tt> - If set to true, the selection will allow multiple choices.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:include_blank</tt> - If set to true, an empty option will be created. If set to a string, the string will be used as the option's content and the value will be empty.
      # * <tt>:prompt</tt> - Create a prompt option with blank value and the text asking user to select something.
      # * Any other key creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   select_tag "people", options_from_collection_for_select(@people, "id", "name")
      #   # <select id="people" name="people"><option value="1">David</option></select>
      #
      #   select_tag "people", options_from_collection_for_select(@people, "id", "name", "1")
      #   # <select id="people" name="people"><option value="1" selected="selected">David</option></select>
      #
      #   select_tag "people", raw("<option>David</option>")
      #   # => <select id="people" name="people"><option>David</option></select>
      #
      #   select_tag "count", raw("<option>1</option><option>2</option><option>3</option><option>4</option>")
      #   # => <select id="count" name="count"><option>1</option><option>2</option>
      #   #    <option>3</option><option>4</option></select>
      #
      #   select_tag "colors", raw("<option>Red</option><option>Green</option><option>Blue</option>"), multiple: true
      #   # => <select id="colors" multiple="multiple" name="colors[]"><option>Red</option>
      #   #    <option>Green</option><option>Blue</option></select>
      #
      #   select_tag "locations", raw("<option>Home</option><option selected='selected'>Work</option><option>Out</option>")
      #   # => <select id="locations" name="locations"><option>Home</option><option selected='selected'>Work</option>
      #   #    <option>Out</option></select>
      #
      #   select_tag "access", raw("<option>Read</option><option>Write</option>"), multiple: true, class: 'form_input', id: 'unique_id'
      #   # => <select class="form_input" id="unique_id" multiple="multiple" name="access[]"><option>Read</option>
      #   #    <option>Write</option></select>
      #
      #   select_tag "people", options_from_collection_for_select(@people, "id", "name"), include_blank: true
      #   # => <select id="people" name="people"><option value="" label=" "></option><option value="1">David</option></select>
      #
      #   select_tag "people", options_from_collection_for_select(@people, "id", "name"), include_blank: "All"
      #   # => <select id="people" name="people"><option value="">All</option><option value="1">David</option></select>
      #
      #   select_tag "people", options_from_collection_for_select(@people, "id", "name"), prompt: "Select something"
      #   # => <select id="people" name="people"><option value="">Select something</option><option value="1">David</option></select>
      #
      #   select_tag "destination", raw("<option>NYC</option><option>Paris</option><option>Rome</option>"), disabled: true
      #   # => <select disabled="disabled" id="destination" name="destination"><option>NYC</option>
      #   #    <option>Paris</option><option>Rome</option></select>
      #
      #   select_tag "credit_card", options_for_select([ "VISA", "MasterCard" ], "MasterCard")
      #   # => <select id="credit_card" name="credit_card"><option>VISA</option>
      #   #    <option selected="selected">MasterCard</option></select>
      def select_tag(name, option_tags = nil, options = {})
        option_tags ||= ""
        html_name = (options[:multiple] == true && !name.end_with?("[]")) ? "#{name}[]" : name

        if options.include?(:include_blank)
          include_blank = options[:include_blank]
          options = options.except(:include_blank)
          options_for_blank_options_tag = { value: "" }

          if include_blank == true
            include_blank = ""
            options_for_blank_options_tag[:label] = " "
          end

          if include_blank
            option_tags = content_tag("option", include_blank, options_for_blank_options_tag).safe_concat(option_tags)
          end
        end

        if prompt = options.delete(:prompt)
          option_tags = content_tag("option", prompt, value: "").safe_concat(option_tags)
        end

        content_tag "select", option_tags, { "name" => html_name, "id" => sanitize_to_id(name) }.update(options.stringify_keys)
      end

      # Creates a standard text field; use these text fields to input smaller chunks of text like a username
      # or a search query.
      #
      # ==== Options
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:size</tt> - The number of visible characters that will fit in the input.
      # * <tt>:maxlength</tt> - The maximum number of characters that the browser will allow the user to enter.
      # * <tt>:placeholder</tt> - The text contained in the field by default which is removed when the field receives focus.
      #   If set to true, use the translation found in the current I18n locale
      #   (through helpers.placeholder.<modelname>.<attribute>).
      # * Any other key creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   text_field_tag 'name'
      #   # => <input id="name" name="name" type="text" />
      #
      #   text_field_tag 'query', 'Enter your search query here'
      #   # => <input id="query" name="query" type="text" value="Enter your search query here" />
      #
      #   text_field_tag 'search', nil, placeholder: 'Enter search term...'
      #   # => <input id="search" name="search" placeholder="Enter search term..." type="text" />
      #
      #   text_field_tag 'request', nil, class: 'special_input'
      #   # => <input class="special_input" id="request" name="request" type="text" />
      #
      #   text_field_tag 'address', '', size: 75
      #   # => <input id="address" name="address" size="75" type="text" value="" />
      #
      #   text_field_tag 'zip', nil, maxlength: 5
      #   # => <input id="zip" maxlength="5" name="zip" type="text" />
      #
      #   text_field_tag 'payment_amount', '$0.00', disabled: true
      #   # => <input disabled="disabled" id="payment_amount" name="payment_amount" type="text" value="$0.00" />
      #
      #   text_field_tag 'ip', '0.0.0.0', maxlength: 15, size: 20, class: "ip-input"
      #   # => <input class="ip-input" id="ip" maxlength="15" name="ip" size="20" type="text" value="0.0.0.0" />
      def text_field_tag(name, value = nil, options = {})
        tag :input, { "type" => "text", "name" => name, "id" => sanitize_to_id(name), "value" => value }.update(options.stringify_keys)
      end

      # Creates a label element. Accepts a block.
      #
      # ==== Options
      # * Creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   label_tag 'name'
      #   # => <label for="name">Name</label>
      #
      #   label_tag 'name', 'Your name'
      #   # => <label for="name">Your name</label>
      #
      #   label_tag 'name', nil, class: 'small_label'
      #   # => <label for="name" class="small_label">Name</label>
      def label_tag(name = nil, content_or_options = nil, options = nil, &block)
        if block_given? && content_or_options.is_a?(Hash)
          options = content_or_options = content_or_options.stringify_keys
        else
          options ||= {}
          options = options.stringify_keys
        end
        options["for"] = sanitize_to_id(name) unless name.blank? || options.has_key?("for")
        content_tag :label, content_or_options || name.to_s.humanize, options, &block
      end

      # Creates a hidden form input field used to transmit data that would be lost due to HTTP's statelessness or
      # data that should be hidden from the user.
      #
      # ==== Options
      # * Creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   hidden_field_tag 'tags_list'
      #   # => <input type="hidden" name="tags_list" id="tags_list" autocomplete="off" />
      #
      #   hidden_field_tag 'token', 'VUBJKB23UIVI1UU1VOBVI@'
      #   # => <input type="hidden" name="token" id="token" value="VUBJKB23UIVI1UU1VOBVI@" autocomplete="off" />
      #
      #   hidden_field_tag 'collected_input', '', onchange: "alert('Input collected!')"
      #   # => <input type="hidden" name="collected_input" id="collected_input"
      #        value="" onchange="alert(&#39;Input collected!&#39;)" autocomplete="off" />
      def hidden_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :hidden, autocomplete: "off"))
      end

      # Creates a file upload field. If you are using file uploads then you will also need
      # to set the multipart option for the form tag:
      #
      #   <%= form_tag '/upload', multipart: true do %>
      #     <label for="file">File to Upload</label> <%= file_field_tag "file" %>
      #     <%= submit_tag %>
      #   <% end %>
      #
      # The specified URL will then be passed a File object containing the selected file, or if the field
      # was left blank, a StringIO object.
      #
      # ==== Options
      # * Creates standard HTML attributes for the tag.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:multiple</tt> - If set to true, *in most updated browsers* the user will be allowed to select multiple files.
      # * <tt>:accept</tt> - If set to one or multiple mime-types, the user will be suggested a filter when choosing a file. You still need to set up model validations.
      #
      # ==== Examples
      #   file_field_tag 'attachment'
      #   # => <input id="attachment" name="attachment" type="file" />
      #
      #   file_field_tag 'avatar', class: 'profile_input'
      #   # => <input class="profile_input" id="avatar" name="avatar" type="file" />
      #
      #   file_field_tag 'picture', disabled: true
      #   # => <input disabled="disabled" id="picture" name="picture" type="file" />
      #
      #   file_field_tag 'resume', value: '~/resume.doc'
      #   # => <input id="resume" name="resume" type="file" value="~/resume.doc" />
      #
      #   file_field_tag 'user_pic', accept: 'image/png,image/gif,image/jpeg'
      #   # => <input accept="image/png,image/gif,image/jpeg" id="user_pic" name="user_pic" type="file" />
      #
      #   file_field_tag 'file', accept: 'text/html', class: 'upload', value: 'index.html'
      #   # => <input accept="text/html" class="upload" id="file" name="file" type="file" value="index.html" />
      def file_field_tag(name, options = {})
        text_field_tag(name, nil, convert_direct_upload_option_to_url(options.merge(type: :file)))
      end

      # Creates a password field, a masked text field that will hide the users input behind a mask character.
      #
      # ==== Options
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:size</tt> - The number of visible characters that will fit in the input.
      # * <tt>:maxlength</tt> - The maximum number of characters that the browser will allow the user to enter.
      # * Any other key creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   password_field_tag 'pass'
      #   # => <input id="pass" name="pass" type="password" />
      #
      #   password_field_tag 'secret', 'Your secret here'
      #   # => <input id="secret" name="secret" type="password" value="Your secret here" />
      #
      #   password_field_tag 'masked', nil, class: 'masked_input_field'
      #   # => <input class="masked_input_field" id="masked" name="masked" type="password" />
      #
      #   password_field_tag 'token', '', size: 15
      #   # => <input id="token" name="token" size="15" type="password" value="" />
      #
      #   password_field_tag 'key', nil, maxlength: 16
      #   # => <input id="key" maxlength="16" name="key" type="password" />
      #
      #   password_field_tag 'confirm_pass', nil, disabled: true
      #   # => <input disabled="disabled" id="confirm_pass" name="confirm_pass" type="password" />
      #
      #   password_field_tag 'pin', '1234', maxlength: 4, size: 6, class: "pin_input"
      #   # => <input class="pin_input" id="pin" maxlength="4" name="pin" size="6" type="password" value="1234" />
      def password_field_tag(name = "password", value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :password))
      end

      # Creates a text input area; use a textarea for longer text inputs such as blog posts or descriptions.
      #
      # ==== Options
      # * <tt>:size</tt> - A string specifying the dimensions (columns by rows) of the textarea (e.g., "25x10").
      # * <tt>:rows</tt> - Specify the number of rows in the textarea
      # * <tt>:cols</tt> - Specify the number of columns in the textarea
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * <tt>:escape</tt> - By default, the contents of the text input are HTML escaped.
      #   If you need unescaped contents, set this to false.
      # * Any other key creates standard HTML attributes for the tag.
      #
      # ==== Examples
      #   text_area_tag 'post'
      #   # => <textarea id="post" name="post"></textarea>
      #
      #   text_area_tag 'bio', @user.bio
      #   # => <textarea id="bio" name="bio">This is my biography.</textarea>
      #
      #   text_area_tag 'body', nil, rows: 10, cols: 25
      #   # => <textarea cols="25" id="body" name="body" rows="10"></textarea>
      #
      #   text_area_tag 'body', nil, size: "25x10"
      #   # => <textarea name="body" id="body" cols="25" rows="10"></textarea>
      #
      #   text_area_tag 'description', "Description goes here.", disabled: true
      #   # => <textarea disabled="disabled" id="description" name="description">Description goes here.</textarea>
      #
      #   text_area_tag 'comment', nil, class: 'comment_input'
      #   # => <textarea class="comment_input" id="comment" name="comment"></textarea>
      def text_area_tag(name, content = nil, options = {})
        options = options.stringify_keys

        if size = options.delete("size")
          options["cols"], options["rows"] = size.split("x") if size.respond_to?(:split)
        end

        escape = options.delete("escape") { true }
        content = ERB::Util.html_escape(content) if escape

        content_tag :textarea, content.to_s.html_safe, { "name" => name, "id" => sanitize_to_id(name) }.update(options)
      end

      ##
      # :call-seq:
      #   check_box_tag(name, options = {})
      #   check_box_tag(name, value, options = {})
      #   check_box_tag(name, value, checked, options = {})
      #
      # Creates a check box form input tag.
      #
      # ==== Options
      # * <tt>:value</tt> - The value of the input. Defaults to <tt>"1"</tt>.
      # * <tt>:checked</tt> - If set to true, the checkbox will be checked by default.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * Any other key creates standard HTML options for the tag.
      #
      # ==== Examples
      #   check_box_tag 'accept'
      #   # => <input id="accept" name="accept" type="checkbox" value="1" />
      #
      #   check_box_tag 'rock', 'rock music'
      #   # => <input id="rock" name="rock" type="checkbox" value="rock music" />
      #
      #   check_box_tag 'receive_email', 'yes', true
      #   # => <input checked="checked" id="receive_email" name="receive_email" type="checkbox" value="yes" />
      #
      #   check_box_tag 'tos', 'yes', false, class: 'accept_tos'
      #   # => <input class="accept_tos" id="tos" name="tos" type="checkbox" value="yes" />
      #
      #   check_box_tag 'eula', 'accepted', false, disabled: true
      #   # => <input disabled="disabled" id="eula" name="eula" type="checkbox" value="accepted" />
      def check_box_tag(name, *args)
        if args.length >= 4
          raise ArgumentError, "wrong number of arguments (given #{args.length + 1}, expected 1..4)"
        end
        options = args.extract_options!
        value, checked = args.empty? ? ["1", false] : [*args, false]
        html_options = { "type" => "checkbox", "name" => name, "id" => sanitize_to_id(name), "value" => value }.update(options.stringify_keys)
        html_options["checked"] = "checked" if checked
        tag :input, html_options
      end

      ##
      # :call-seq:
      #   radio_button_tag(name, value, options = {})
      #   radio_button_tag(name, value, checked, options = {})
      #
      # Creates a radio button; use groups of radio buttons named the same to allow users to
      # select from a group of options.
      #
      # ==== Options
      # * <tt>:checked</tt> - If set to true, the radio button will be selected by default.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * Any other key creates standard HTML options for the tag.
      #
      # ==== Examples
      #   radio_button_tag 'favorite_color', 'maroon'
      #   # => <input id="favorite_color_maroon" name="favorite_color" type="radio" value="maroon" />
      #
      #   radio_button_tag 'receive_updates', 'no', true
      #   # => <input checked="checked" id="receive_updates_no" name="receive_updates" type="radio" value="no" />
      #
      #   radio_button_tag 'time_slot', "3:00 p.m.", false, disabled: true
      #   # => <input disabled="disabled" id="time_slot_3:00_p.m." name="time_slot" type="radio" value="3:00 p.m." />
      #
      #   radio_button_tag 'color', "green", true, class: "color_input"
      #   # => <input checked="checked" class="color_input" id="color_green" name="color" type="radio" value="green" />
      def radio_button_tag(name, value, *args)
        if args.length >= 3
          raise ArgumentError, "wrong number of arguments (given #{args.length + 2}, expected 2..4)"
        end
        options = args.extract_options!
        checked = args.empty? ? false : args.first
        html_options = { "type" => "radio", "name" => name, "id" => "#{sanitize_to_id(name)}_#{sanitize_to_id(value)}", "value" => value }.update(options.stringify_keys)
        html_options["checked"] = "checked" if checked
        tag :input, html_options
      end

      # Creates a submit button with the text <tt>value</tt> as the caption.
      #
      # ==== Options
      # * <tt>:data</tt> - This option can be used to add custom data attributes.
      # * <tt>:disabled</tt> - If true, the user will not be able to use this input.
      # * Any other key creates standard HTML options for the tag.
      #
      # ==== Examples
      #   submit_tag
      #   # => <input name="commit" data-disable-with="Save changes" type="submit" value="Save changes" />
      #
      #   submit_tag "Edit this article"
      #   # => <input name="commit" data-disable-with="Edit this article" type="submit" value="Edit this article" />
      #
      #   submit_tag "Save edits", disabled: true
      #   # => <input disabled="disabled" name="commit" data-disable-with="Save edits" type="submit" value="Save edits" />
      #
      #   submit_tag nil, class: "form_submit"
      #   # => <input class="form_submit" name="commit" type="submit" />
      #
      #   submit_tag "Edit", class: "edit_button"
      #   # => <input class="edit_button" data-disable-with="Edit" name="commit" type="submit" value="Edit" />
      #
      # ==== Deprecated: \Rails UJS attributes
      #
      # Prior to \Rails 7, \Rails shipped with the JavaScript library called @rails/ujs on by default. Following \Rails 7,
      # this library is no longer on by default. This library integrated with the following options:
      #
      # * <tt>confirm: 'question?'</tt> - If present the unobtrusive JavaScript
      #   drivers will provide a prompt with the question specified. If the user accepts,
      #   the form is processed normally, otherwise no action is taken.
      # * <tt>:disable_with</tt> - Value of this parameter will be used as the value for a
      #   disabled version of the submit button when the form is submitted. This feature is
      #   provided by the unobtrusive JavaScript driver. To disable this feature for a single submit tag
      #   pass <tt>:data => { disable_with: false }</tt> Defaults to value attribute.
      #
      #   submit_tag "Complete sale", data: { disable_with: "Submitting..." }
      #   # => <input name="commit" data-disable-with="Submitting..." type="submit" value="Complete sale" />
      #
      #   submit_tag "Save", data: { confirm: "Are you sure?" }
      #   # => <input name='commit' type='submit' value='Save' data-disable-with="Save" data-confirm="Are you sure?" />
      #
      def submit_tag(value = "Save changes", options = {})
        options = options.deep_stringify_keys
        tag_options = { "type" => "submit", "name" => "commit", "value" => value }.update(options)
        set_default_disable_with value, tag_options
        tag :input, tag_options
      end

      # Creates a button element that defines a <tt>submit</tt> button,
      # <tt>reset</tt> button or a generic button which can be used in
      # JavaScript, for example. You can use the button tag as a regular
      # submit tag but it isn't supported in legacy browsers. However,
      # the button tag does allow for richer labels such as images and emphasis,
      # so this helper will also accept a block. By default, it will create
      # a button tag with type <tt>submit</tt>, if type is not given.
      #
      # ==== Options
      # * <tt>:data</tt> - This option can be used to add custom data attributes.
      # * <tt>:disabled</tt> - If true, the user will not be able to
      #   use this input.
      # * Any other key creates standard HTML options for the tag.
      #
      # ==== Examples
      #   button_tag
      #   # => <button name="button" type="submit">Button</button>
      #
      #   button_tag 'Reset', type: 'reset'
      #   # => <button name="button" type="reset">Reset</button>
      #
      #   button_tag 'Button', type: 'button'
      #   # => <button name="button" type="button">Button</button>
      #
      #   button_tag 'Reset', type: 'reset', disabled: true
      #   # => <button name="button" type="reset" disabled="disabled">Reset</button>
      #
      #   button_tag(type: 'button') do
      #     content_tag(:strong, 'Ask me!')
      #   end
      #   # => <button name="button" type="button">
      #   #     <strong>Ask me!</strong>
      #   #    </button>
      #
      # ==== Deprecated: \Rails UJS attributes
      #
      # Prior to \Rails 7, \Rails shipped with a JavaScript library called @rails/ujs on by default. Following \Rails 7,
      # this library is no longer on by default. This library integrated with the following options:
      #
      # * <tt>confirm: 'question?'</tt> - If present, the
      #   unobtrusive JavaScript drivers will provide a prompt with
      #   the question specified. If the user accepts, the form is
      #   processed normally, otherwise no action is taken.
      # * <tt>:disable_with</tt> - Value of this parameter will be
      #   used as the value for a disabled version of the submit
      #   button when the form is submitted. This feature is provided
      #   by the unobtrusive JavaScript driver.
      #
      #   button_tag "Save", data: { confirm: "Are you sure?" }
      #   # => <button name="button" type="submit" data-confirm="Are you sure?">Save</button>
      #
      #   button_tag "Checkout", data: { disable_with: "Please wait..." }
      #   # => <button data-disable-with="Please wait..." name="button" type="submit">Checkout</button>
      #
      def button_tag(content_or_options = nil, options = nil, &block)
        if content_or_options.is_a? Hash
          options = content_or_options
        else
          options ||= {}
        end

        options = { "name" => "button", "type" => "submit" }.merge!(options.stringify_keys)

        if block_given?
          content_tag :button, options, &block
        else
          content_tag :button, content_or_options || "Button", options
        end
      end

      # Displays an image which when clicked will submit the form.
      #
      # <tt>source</tt> is passed to AssetTagHelper#path_to_image
      #
      # ==== Options
      # * <tt>:data</tt> - This option can be used to add custom data attributes.
      # * <tt>:disabled</tt> - If set to true, the user will not be able to use this input.
      # * Any other key creates standard HTML options for the tag.
      #
      # ==== Data attributes
      #
      # * <tt>confirm: 'question?'</tt> - This will add a JavaScript confirm
      #   prompt with the question specified. If the user accepts, the form is
      #   processed normally, otherwise no action is taken.
      #
      # ==== Examples
      #   image_submit_tag("login.png")
      #   # => <input src="/assets/login.png" type="image" />
      #
      #   image_submit_tag("purchase.png", disabled: true)
      #   # => <input disabled="disabled" src="/assets/purchase.png" type="image" />
      #
      #   image_submit_tag("search.png", class: 'search_button', alt: 'Find')
      #   # => <input class="search_button" src="/assets/search.png" type="image" />
      #
      #   image_submit_tag("agree.png", disabled: true, class: "agree_disagree_button")
      #   # => <input class="agree_disagree_button" disabled="disabled" src="/assets/agree.png" type="image" />
      #
      #   image_submit_tag("save.png", data: { confirm: "Are you sure?" })
      #   # => <input src="/assets/save.png" data-confirm="Are you sure?" type="image" />
      def image_submit_tag(source, options = {})
        options = options.stringify_keys
        src = path_to_image(source, skip_pipeline: options.delete("skip_pipeline"))
        tag :input, { "type" => "image", "src" => src }.update(options)
      end

      # Creates a field set for grouping HTML form elements.
      #
      # <tt>legend</tt> will become the fieldset's title (optional as per W3C).
      # <tt>options</tt> accept the same values as tag.
      #
      # ==== Examples
      #   <%= field_set_tag do %>
      #     <p><%= text_field_tag 'name' %></p>
      #   <% end %>
      #   # => <fieldset><p><input id="name" name="name" type="text" /></p></fieldset>
      #
      #   <%= field_set_tag 'Your details' do %>
      #     <p><%= text_field_tag 'name' %></p>
      #   <% end %>
      #   # => <fieldset><legend>Your details</legend><p><input id="name" name="name" type="text" /></p></fieldset>
      #
      #   <%= field_set_tag nil, class: 'format' do %>
      #     <p><%= text_field_tag 'name' %></p>
      #   <% end %>
      #   # => <fieldset class="format"><p><input id="name" name="name" type="text" /></p></fieldset>
      def field_set_tag(legend = nil, options = nil, &block)
        output = tag(:fieldset, options, true)
        output.safe_concat(content_tag("legend", legend)) unless legend.blank?
        output.concat(capture(&block)) if block_given?
        output.safe_concat("</fieldset>")
      end

      # Creates a text field of type "color".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   color_field_tag 'name'
      #   # => <input id="name" name="name" type="color" />
      #
      #   color_field_tag 'color', '#DEF726'
      #   # => <input id="color" name="color" type="color" value="#DEF726" />
      #
      #   color_field_tag 'color', nil, class: 'special_input'
      #   # => <input class="special_input" id="color" name="color" type="color" />
      #
      #   color_field_tag 'color', '#DEF726', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="color" name="color" type="color" value="#DEF726" />
      def color_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :color))
      end

      # Creates a text field of type "search".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   search_field_tag 'name'
      #   # => <input id="name" name="name" type="search" />
      #
      #   search_field_tag 'search', 'Enter your search query here'
      #   # => <input id="search" name="search" type="search" value="Enter your search query here" />
      #
      #   search_field_tag 'search', nil, class: 'special_input'
      #   # => <input class="special_input" id="search" name="search" type="search" />
      #
      #   search_field_tag 'search', 'Enter your search query here', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="search" name="search" type="search" value="Enter your search query here" />
      def search_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :search))
      end

      # Creates a text field of type "tel".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   telephone_field_tag 'name'
      #   # => <input id="name" name="name" type="tel" />
      #
      #   telephone_field_tag 'tel', '0123456789'
      #   # => <input id="tel" name="tel" type="tel" value="0123456789" />
      #
      #   telephone_field_tag 'tel', nil, class: 'special_input'
      #   # => <input class="special_input" id="tel" name="tel" type="tel" />
      #
      #   telephone_field_tag 'tel', '0123456789', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="tel" name="tel" type="tel" value="0123456789" />
      def telephone_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :tel))
      end
      alias phone_field_tag telephone_field_tag

      # Creates a text field of type "date".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   date_field_tag 'name'
      #   # => <input id="name" name="name" type="date" />
      #
      #   date_field_tag 'date', '01/01/2014'
      #   # => <input id="date" name="date" type="date" value="01/01/2014" />
      #
      #   date_field_tag 'date', nil, class: 'special_input'
      #   # => <input class="special_input" id="date" name="date" type="date" />
      #
      #   date_field_tag 'date', '01/01/2014', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="date" name="date" type="date" value="01/01/2014" />
      def date_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :date))
      end

      # Creates a text field of type "time".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag. Additionally, supports:
      #
      # * <tt>:min</tt> - The minimum acceptable value.
      # * <tt>:max</tt> - The maximum acceptable value.
      # * <tt>:step</tt> - The acceptable value granularity.
      # * <tt>:include_seconds</tt> - Include seconds and ms in the output timestamp format (true by default).
      def time_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :time))
      end

      # Creates a text field of type "datetime-local".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag. Additionally, supports:
      #
      # * <tt>:min</tt> - The minimum acceptable value.
      # * <tt>:max</tt> - The maximum acceptable value.
      # * <tt>:step</tt> - The acceptable value granularity.
      # * <tt>:include_seconds</tt> - Include seconds in the output timestamp format (true by default).
      def datetime_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: "datetime-local"))
      end

      alias datetime_local_field_tag datetime_field_tag

      # Creates a text field of type "month".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag. Additionally, supports:
      #
      # * <tt>:min</tt> - The minimum acceptable value.
      # * <tt>:max</tt> - The maximum acceptable value.
      # * <tt>:step</tt> - The acceptable value granularity.
      def month_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :month))
      end

      # Creates a text field of type "week".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag. Additionally, supports:
      #
      # * <tt>:min</tt> - The minimum acceptable value.
      # * <tt>:max</tt> - The maximum acceptable value.
      # * <tt>:step</tt> - The acceptable value granularity.
      def week_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :week))
      end

      # Creates a text field of type "url".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   url_field_tag 'name'
      #   # => <input id="name" name="name" type="url" />
      #
      #   url_field_tag 'url', 'http://rubyonrails.org'
      #   # => <input id="url" name="url" type="url" value="http://rubyonrails.org" />
      #
      #   url_field_tag 'url', nil, class: 'special_input'
      #   # => <input class="special_input" id="url" name="url" type="url" />
      #
      #   url_field_tag 'url', 'http://rubyonrails.org', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="url" name="url" type="url" value="http://rubyonrails.org" />
      def url_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :url))
      end

      # Creates a text field of type "email".
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag.
      #
      # ==== Examples
      #
      #   email_field_tag 'name'
      #   # => <input id="name" name="name" type="email" />
      #
      #   email_field_tag 'email', 'email@example.com'
      #   # => <input id="email" name="email" type="email" value="email@example.com" />
      #
      #   email_field_tag 'email', nil, class: 'special_input'
      #   # => <input class="special_input" id="email" name="email" type="email" />
      #
      #   email_field_tag 'email', 'email@example.com', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="email" name="email" type="email" value="email@example.com" />
      def email_field_tag(name, value = nil, options = {})
        text_field_tag(name, value, options.merge(type: :email))
      end

      # Creates a number field.
      #
      # ==== Options
      #
      # Supports the same options as #text_field_tag. Additionally, supports:
      #
      # * <tt>:min</tt> - The minimum acceptable value.
      # * <tt>:max</tt> - The maximum acceptable value.
      # * <tt>:in</tt> - A range specifying the <tt>:min</tt> and
      #   <tt>:max</tt> values.
      # * <tt>:within</tt> - Same as <tt>:in</tt>.
      # * <tt>:step</tt> - The acceptable value granularity.
      #
      # ==== Examples
      #
      #   number_field_tag 'quantity'
      #   # => <input id="quantity" name="quantity" type="number" />
      #
      #   number_field_tag 'quantity', '1'
      #   # => <input id="quantity" name="quantity" type="number" value="1" />
      #
      #   number_field_tag 'quantity', nil, class: 'special_input'
      #   # => <input class="special_input" id="quantity" name="quantity" type="number" />
      #
      #   number_field_tag 'quantity', nil, min: 1
      #   # => <input id="quantity" name="quantity" min="1" type="number" />
      #
      #   number_field_tag 'quantity', nil, max: 9
      #   # => <input id="quantity" name="quantity" max="9" type="number" />
      #
      #   number_field_tag 'quantity', nil, in: 1...10
      #   # => <input id="quantity" name="quantity" min="1" max="9" type="number" />
      #
      #   number_field_tag 'quantity', nil, within: 1...10
      #   # => <input id="quantity" name="quantity" min="1" max="9" type="number" />
      #
      #   number_field_tag 'quantity', nil, min: 1, max: 10
      #   # => <input id="quantity" name="quantity" min="1" max="10" type="number" />
      #
      #   number_field_tag 'quantity', nil, min: 1, max: 10, step: 2
      #   # => <input id="quantity" name="quantity" min="1" max="10" step="2" type="number" />
      #
      #   number_field_tag 'quantity', '1', class: 'special_input', disabled: true
      #   # => <input disabled="disabled" class="special_input" id="quantity" name="quantity" type="number" value="1" />
      def number_field_tag(name, value = nil, options = {})
        options = options.stringify_keys
        options["type"] ||= "number"
        if range = options.delete("in") || options.delete("within")
          options.update("min" => range.min, "max" => range.max)
        end
        text_field_tag(name, value, options)
      end

      # Creates a range form element.
      #
      # ==== Options
      #
      # Supports the same options as #number_field_tag.
      def range_field_tag(name, value = nil, options = {})
        number_field_tag(name, value, options.merge(type: :range))
      end

      # Creates the hidden UTF-8 enforcer tag. Override this method in a helper
      # to customize the tag.
      def utf8_enforcer_tag
        # Use raw HTML to ensure the value is written as an HTML entity; it
        # needs to be the right character regardless of which encoding the
        # browser infers.
        '<input name="utf8" type="hidden" value="&#x2713;" autocomplete="off" />'.html_safe
      end

      private
        def html_options_for_form(url_for_options, options)
          options.stringify_keys.tap do |html_options|
            html_options["enctype"] = "multipart/form-data" if html_options.delete("multipart")
            # The following URL is unescaped, this is just a hash of options, and it is the
            # responsibility of the caller to escape all the values.
            if url_for_options == false || html_options["action"] == false
              html_options.delete("action")
            else
              html_options["action"] = url_for(url_for_options)
            end
            html_options["accept-charset"] = "UTF-8"

            html_options["data-remote"] = true if html_options.delete("remote")

            if html_options["data-remote"] &&
               embed_authenticity_token_in_remote_forms == false &&
               html_options["authenticity_token"].blank?
              # The authenticity token is taken from the meta tag in this case
              html_options["authenticity_token"] = false
            elsif html_options["authenticity_token"] == true
              # Include the default authenticity_token, which is only generated when its set to nil,
              # but we needed the true value to override the default of no authenticity_token on data-remote.
              html_options["authenticity_token"] = nil
            end
          end
        end

        def extra_tags_for_form(html_options)
          authenticity_token = html_options.delete("authenticity_token")
          method = html_options.delete("method").to_s.downcase

          method_tag = \
            case method
            when "get"
              html_options["method"] = "get"
              ""
            when "post", ""
              html_options["method"] = "post"
              token_tag(authenticity_token, form_options: {
                action: html_options["action"],
                method: "post"
              })
            else
              html_options["method"] = "post"
              method_tag(method) + token_tag(authenticity_token, form_options: {
                action: html_options["action"],
                method: method
              })
            end

          if html_options.delete("enforce_utf8") { default_enforce_utf8 }
            utf8_enforcer_tag + method_tag
          else
            method_tag
          end
        end

        def form_tag_html(html_options)
          extra_tags = extra_tags_for_form(html_options)
          html = tag(:form, html_options, true) + extra_tags
          prevent_content_exfiltration(html)
        end

        def form_tag_with_body(html_options, content)
          output = form_tag_html(html_options)
          output << content.to_s if content
          output.safe_concat("</form>")
        end

        # see http://www.w3.org/TR/html4/types.html#type-name
        def sanitize_to_id(name)
          name.to_s.delete("]").tr("^-a-zA-Z0-9:.", "_")
        end

        def set_default_disable_with(value, tag_options)
          data = tag_options.fetch("data", {})

          if tag_options["data-disable-with"] == false || data["disable_with"] == false
            data.delete("disable_with")
          elsif ActionView::Base.automatically_disable_submit_tag
            disable_with_text = tag_options["data-disable-with"]
            disable_with_text ||= data["disable_with"]
            disable_with_text ||= value.to_s.clone
            tag_options.deep_merge!("data" => { "disable_with" => disable_with_text })
          end

          tag_options.delete("data-disable-with")
        end

        def convert_direct_upload_option_to_url(options)
          return options unless options.delete(:direct_upload)

          if respond_to?(:rails_direct_uploads_url)
            options["data-direct-upload-url"] = rails_direct_uploads_url
          elsif respond_to?(:main_app) && main_app.respond_to?(:rails_direct_uploads_url)
            options["data-direct-upload-url"] = main_app.rails_direct_uploads_url
          end

          options
        end
    end
  end
end


// --- rb_railties_app.rb ---
# frozen_string_literal: true

require "yaml"
require "active_support/core_ext/hash/keys"
require "active_support/core_ext/object/blank"
require "active_support/key_generator"
require "active_support/message_verifiers"
require "active_support/deprecation"
require "active_support/encrypted_configuration"
require "active_support/hash_with_indifferent_access"
require "active_support/configuration_file"
require "rails/engine"
require "rails/secrets"
require "rails/autoloaders"

module Rails
  # An Engine with the responsibility of coordinating the whole boot process.
  #
  # == Initialization
  #
  # Rails::Application is responsible for executing all railties and engines
  # initializers. It also executes some bootstrap initializers (check
  # Rails::Application::Bootstrap) and finishing initializers, after all the others
  # are executed (check Rails::Application::Finisher).
  #
  # == \Configuration
  #
  # Besides providing the same configuration as Rails::Engine and Rails::Railtie,
  # the application object has several specific configurations, for example
  # +enable_reloading+, +consider_all_requests_local+, +filter_parameters+,
  # +logger+, and so forth.
  #
  # Check Rails::Application::Configuration to see them all.
  #
  # == Routes
  #
  # The application object is also responsible for holding the routes and reloading routes
  # whenever the files change in development.
  #
  # == Middlewares
  #
  # The Application is also responsible for building the middleware stack.
  #
  # == Booting process
  #
  # The application is also responsible for setting up and executing the booting
  # process. From the moment you require <tt>config/application.rb</tt> in your app,
  # the booting process goes like this:
  #
  # 1.  <tt>require "config/boot.rb"</tt> to set up load paths.
  # 2.  +require+ railties and engines.
  # 3.  Define +Rails.application+ as <tt>class MyApp::Application < Rails::Application</tt>.
  # 4.  Run +config.before_configuration+ callbacks.
  # 5.  Load <tt>config/environments/ENV.rb</tt>.
  # 6.  Run +config.before_initialize+ callbacks.
  # 7.  Run <tt>Railtie#initializer</tt> defined by railties, engines, and application.
  #     One by one, each engine sets up its load paths and routes, and runs its <tt>config/initializers/*</tt> files.
  # 8.  Custom <tt>Railtie#initializers</tt> added by railties, engines, and applications are executed.
  # 9.  Build the middleware stack and run +to_prepare+ callbacks.
  # 10. Run +config.before_eager_load+ and +eager_load!+ if +eager_load+ is +true+.
  # 11. Run +config.after_initialize+ callbacks.
  class Application < Engine
    autoload :Bootstrap,              "rails/application/bootstrap"
    autoload :Configuration,          "rails/application/configuration"
    autoload :DefaultMiddlewareStack, "rails/application/default_middleware_stack"
    autoload :Finisher,               "rails/application/finisher"
    autoload :Railties,               "rails/engine/railties"
    autoload :RoutesReloader,         "rails/application/routes_reloader"

    class << self
      def inherited(base)
        super
        Rails.app_class = base
        # lib has to be added to $LOAD_PATH unconditionally, even if it's in the
        # autoload paths and config.add_autoload_paths_to_load_path is false.
        add_lib_to_load_path!(find_root(base.called_from))
        ActiveSupport.run_load_hooks(:before_configuration, base)
      end

      def instance
        super.run_load_hooks!
      end

      def create(initial_variable_values = {}, &block)
        new(initial_variable_values, &block).run_load_hooks!
      end

      def find_root(from)
        find_root_with_flag "config.ru", from, Dir.pwd
      end

      # Makes the +new+ method public.
      #
      # Note that Rails::Application inherits from Rails::Engine, which
      # inherits from Rails::Railtie and the +new+ method on Rails::Railtie is
      # private
      public :new
    end

    attr_accessor :assets, :sandbox
    alias_method :sandbox?, :sandbox
    attr_reader :reloaders, :reloader, :executor, :autoloaders

    delegate :default_url_options, :default_url_options=, to: :routes

    INITIAL_VARIABLES = [:config, :railties, :routes_reloader, :reloaders,
                         :routes, :helpers, :app_env_config, :secrets] # :nodoc:

    def initialize(initial_variable_values = {}, &block)
      super()
      @initialized       = false
      @reloaders         = []
      @routes_reloader   = nil
      @app_env_config    = nil
      @ordered_railties  = nil
      @railties          = nil
      @key_generators    = {}
      @message_verifiers = nil
      @deprecators       = nil
      @ran_load_hooks    = false

      @executor          = Class.new(ActiveSupport::Executor)
      @reloader          = Class.new(ActiveSupport::Reloader)
      @reloader.executor = @executor

      @autoloaders = Rails::Autoloaders.new

      # are these actually used?
      @initial_variable_values = initial_variable_values
      @block = block
    end

    # Returns true if the application is initialized.
    def initialized?
      @initialized
    end

    def run_load_hooks! # :nodoc:
      return self if @ran_load_hooks
      @ran_load_hooks = true

      @initial_variable_values.each do |variable_name, value|
        if INITIAL_VARIABLES.include?(variable_name)
          instance_variable_set("@#{variable_name}", value)
        end
      end

      instance_eval(&@block) if @block
      self
    end

    # Reload application routes regardless if they changed or not.
    def reload_routes!
      routes_reloader.reload!
    end

    # Returns a key generator (ActiveSupport::CachingKeyGenerator) for a
    # specified +secret_key_base+. The return value is memoized, so additional
    # calls with the same +secret_key_base+ will return the same key generator
    # instance.
    def key_generator(secret_key_base = self.secret_key_base)
      # number of iterations selected based on consultation with the google security
      # team. Details at https://github.com/rails/rails/pull/6952#issuecomment-7661220
      @key_generators[secret_key_base] ||= ActiveSupport::CachingKeyGenerator.new(
        ActiveSupport::KeyGenerator.new(secret_key_base, iterations: 1000)
      )
    end

    # Returns a message verifier factory (ActiveSupport::MessageVerifiers). This
    # factory can be used as a central point to configure and create message
    # verifiers (ActiveSupport::MessageVerifier) for your application.
    #
    # By default, message verifiers created by this factory will generate
    # messages using the default ActiveSupport::MessageVerifier options. You can
    # override these options with a combination of
    # ActiveSupport::MessageVerifiers#clear_rotations and
    # ActiveSupport::MessageVerifiers#rotate. However, this must be done prior
    # to building any message verifier instances. For example, in a
    # +before_initialize+ block:
    #
    #   # Use `url_safe: true` when generating messages
    #   config.before_initialize do |app|
    #     app.message_verifiers.clear_rotations
    #     app.message_verifiers.rotate(url_safe: true)
    #   end
    #
    # Message verifiers created by this factory will always use a secret derived
    # from #secret_key_base when generating messages. +clear_rotations+ will not
    # affect this behavior. However, older +secret_key_base+ values can be
    # rotated for verifying messages:
    #
    #   # Fall back to old `secret_key_base` when verifying messages
    #   config.before_initialize do |app|
    #     app.message_verifiers.rotate(secret_key_base: "old secret_key_base")
    #   end
    #
    def message_verifiers
      @message_verifiers ||=
        ActiveSupport::MessageVerifiers.new do |salt, secret_key_base: self.secret_key_base|
          key_generator(secret_key_base).generate_key(salt)
        end.rotate_defaults
    end

    # Returns a message verifier object.
    #
    # This verifier can be used to generate and verify signed messages in the application.
    #
    # It is recommended not to use the same verifier for different things, so you can get different
    # verifiers passing the +verifier_name+ argument.
    #
    # ==== Parameters
    #
    # * +verifier_name+ - the name of the message verifier.
    #
    # ==== Examples
    #
    #     message = Rails.application.message_verifier('sensitive_data').generate('my sensible data')
    #     Rails.application.message_verifier('sensitive_data').verify(message)
    #     # => 'my sensible data'
    #
    # See the ActiveSupport::MessageVerifier documentation for more information.
    def message_verifier(verifier_name)
      message_verifiers[verifier_name]
    end

    # A managed collection of deprecators (ActiveSupport::Deprecation::Deprecators).
    # The collection's configuration methods affect all deprecators in the
    # collection. Additionally, the collection's +silence+ method silences all
    # deprecators in the collection for the duration of a given block.
    def deprecators
      @deprecators ||= ActiveSupport::Deprecation::Deprecators.new.tap do |deprecators|
        deprecators[:railties] = Rails.deprecator
      end
    end

    # Convenience for loading config/foo.yml for the current \Rails env.
    # Example:
    #
    #     # config/exception_notification.yml:
    #     production:
    #       url: http://127.0.0.1:8080
    #       namespace: my_app_production
    #
    #     development:
    #       url: http://localhost:3001
    #       namespace: my_app_development
    #
    # <code></code>
    #
    #     # config/environments/production.rb
    #     Rails.application.configure do
    #       config.middleware.use ExceptionNotifier, config_for(:exception_notification)
    #     end
    #
    # You can also store configurations in a shared section which will be merged
    # with the environment configuration
    #
    #     # config/example.yml
    #     shared:
    #       foo:
    #         bar:
    #           baz: 1
    #
    #     development:
    #       foo:
    #         bar:
    #           qux: 2
    #
    # <code></code>
    #
    #     # development environment
    #     Rails.application.config_for(:example)[:foo][:bar]
    #     # => { baz: 1, qux: 2 }
    def config_for(name, env: Rails.env)
      yaml = name.is_a?(Pathname) ? name : Pathname.new("#{paths["config"].existent.first}/#{name}.yml")

      if yaml.exist?
        require "erb"
        all_configs    = ActiveSupport::ConfigurationFile.parse(yaml).deep_symbolize_keys
        config, shared = all_configs[env.to_sym], all_configs[:shared]

        if shared
          config = {} if config.nil? && shared.is_a?(Hash)
          if config.is_a?(Hash) && shared.is_a?(Hash)
            config = shared.deep_merge(config)
          elsif config.nil?
            config = shared
          end
        end

        if config.is_a?(Hash)
          config = ActiveSupport::OrderedOptions.new.update(config)
        end

        config
      else
        raise "Could not load configuration. No such file - #{yaml}"
      end
    end

    # Stores some of the \Rails initial environment parameters which
    # will be used by middlewares and engines to configure themselves.
    def env_config
      @app_env_config ||= super.merge(
          "action_dispatch.parameter_filter" => filter_parameters,
          "action_dispatch.redirect_filter" => config.filter_redirect,
          "action_dispatch.secret_key_base" => secret_key_base,
          "action_dispatch.show_exceptions" => config.action_dispatch.show_exceptions,
          "action_dispatch.show_detailed_exceptions" => config.consider_all_requests_local,
          "action_dispatch.log_rescued_responses" => config.action_dispatch.log_rescued_responses,
          "action_dispatch.debug_exception_log_level" => ActiveSupport::Logger.const_get(config.action_dispatch.debug_exception_log_level.to_s.upcase),
          "action_dispatch.logger" => Rails.logger,
          "action_dispatch.backtrace_cleaner" => Rails.backtrace_cleaner,
          "action_dispatch.key_generator" => key_generator,
          "action_dispatch.http_auth_salt" => config.action_dispatch.http_auth_salt,
          "action_dispatch.signed_cookie_salt" => config.action_dispatch.signed_cookie_salt,
          "action_dispatch.encrypted_cookie_salt" => config.action_dispatch.encrypted_cookie_salt,
          "action_dispatch.encrypted_signed_cookie_salt" => config.action_dispatch.encrypted_signed_cookie_salt,
          "action_dispatch.authenticated_encrypted_cookie_salt" => config.action_dispatch.authenticated_encrypted_cookie_salt,
          "action_dispatch.use_authenticated_cookie_encryption" => config.action_dispatch.use_authenticated_cookie_encryption,
          "action_dispatch.encrypted_cookie_cipher" => config.action_dispatch.encrypted_cookie_cipher,
          "action_dispatch.signed_cookie_digest" => config.action_dispatch.signed_cookie_digest,
          "action_dispatch.cookies_serializer" => config.action_dispatch.cookies_serializer,
          "action_dispatch.cookies_digest" => config.action_dispatch.cookies_digest,
          "action_dispatch.cookies_rotations" => config.action_dispatch.cookies_rotations,
          "action_dispatch.cookies_same_site_protection" => coerce_same_site_protection(config.action_dispatch.cookies_same_site_protection),
          "action_dispatch.use_cookies_with_metadata" => config.action_dispatch.use_cookies_with_metadata,
          "action_dispatch.content_security_policy" => config.content_security_policy,
          "action_dispatch.content_security_policy_report_only" => config.content_security_policy_report_only,
          "action_dispatch.content_security_policy_nonce_generator" => config.content_security_policy_nonce_generator,
          "action_dispatch.content_security_policy_nonce_directives" => config.content_security_policy_nonce_directives,
          "action_dispatch.permissions_policy" => config.permissions_policy,
        )
    end

    # If you try to define a set of Rake tasks on the instance, these will get
    # passed up to the Rake tasks defined on the application's class.
    def rake_tasks(&block)
      self.class.rake_tasks(&block)
    end

    # Sends the initializers to the +initializer+ method defined in the
    # Rails::Initializable module. Each Rails::Application class has its own
    # set of initializers, as defined by the Initializable module.
    def initializer(name, opts = {}, &block)
      self.class.initializer(name, opts, &block)
    end

    # Sends any runner called in the instance of a new application up
    # to the +runner+ method defined in Rails::Railtie.
    def runner(&blk)
      self.class.runner(&blk)
    end

    # Sends any console called in the instance of a new application up
    # to the +console+ method defined in Rails::Railtie.
    def console(&blk)
      self.class.console(&blk)
    end

    # Sends any generators called in the instance of a new application up
    # to the +generators+ method defined in Rails::Railtie.
    def generators(&blk)
      self.class.generators(&blk)
    end

    # Sends any server called in the instance of a new application up
    # to the +server+ method defined in Rails::Railtie.
    def server(&blk)
      self.class.server(&blk)
    end

    # Sends the +isolate_namespace+ method up to the class method.
    def isolate_namespace(mod)
      self.class.isolate_namespace(mod)
    end

    ## Rails internal API

    # This method is called just after an application inherits from Rails::Application,
    # allowing the developer to load classes in lib and use them during application
    # configuration.
    #
    #   class MyApplication < Rails::Application
    #     require "my_backend" # in lib/my_backend
    #     config.i18n.backend = MyBackend
    #   end
    #
    # Notice this method takes into consideration the default root path. So if you
    # are changing config.root inside your application definition or having a custom
    # Rails application, you will need to add lib to $LOAD_PATH on your own in case
    # you need to load files in lib/ during the application configuration as well.
    def self.add_lib_to_load_path!(root) # :nodoc:
      path = File.join(root, "lib")
      if File.exist?(path) && !$LOAD_PATH.include?(path)
        $LOAD_PATH.unshift(path)
      end
    end

    def require_environment! # :nodoc:
      environment = paths["config/environment"].existent.first
      require environment if environment
    end

    def routes_reloader # :nodoc:
      @routes_reloader ||= RoutesReloader.new
    end

    # Returns an array of file paths appended with a hash of
    # directories-extensions suitable for ActiveSupport::FileUpdateChecker
    # API.
    def watchable_args # :nodoc:
      files, dirs = config.watchable_files.dup, config.watchable_dirs.dup

      ActiveSupport::Dependencies.autoload_paths.each do |path|
        File.file?(path) ? files << path.to_s : dirs[path.to_s] = [:rb]
      end

      [files, dirs]
    end

    # Initialize the application passing the given group. By default, the
    # group is :default
    def initialize!(group = :default) # :nodoc:
      raise "Application has been already initialized." if @initialized
      run_initializers(group, self)
      @initialized = true
      self
    end

    def initializers # :nodoc:
      Bootstrap.initializers_for(self) +
      railties_initializers(super) +
      Finisher.initializers_for(self)
    end

    def config # :nodoc:
      @config ||= Application::Configuration.new(self.class.find_root(self.class.called_from))
    end

    attr_writer :config

    def secrets
      Rails.deprecator.warn(<<~MSG.squish)
        `Rails.application.secrets` is deprecated in favor of `Rails.application.credentials` and will be removed in Rails 7.2.
      MSG
      @secrets ||= begin
        secrets = ActiveSupport::OrderedOptions.new
        files = config.paths["config/secrets"].existent
        files = files.reject { |path| path.end_with?(".enc") } unless config.read_encrypted_secrets
        secrets.merge! Rails::Secrets.parse(files, env: Rails.env)

        # Fallback to config.secret_key_base if secrets.secret_key_base isn't set
        secrets.secret_key_base ||= config.secret_key_base

        secrets
      end
    end

    attr_writer :secrets, :credentials

    # The secret_key_base is used as the input secret to the application's key generator, which in turn
    # is used to create all ActiveSupport::MessageVerifier and ActiveSupport::MessageEncryptor instances,
    # including the ones that sign and encrypt cookies.
    #
    # In development and test, this is randomly generated and stored in a
    # temporary file in <tt>tmp/local_secret.txt</tt>.
    #
    # You can also set <tt>ENV["SECRET_KEY_BASE_DUMMY"]</tt> to trigger the use of a randomly generated
    # secret_key_base that's stored in a temporary file. This is useful when precompiling assets for
    # production as part of a build step that otherwise does not need access to the production secrets.
    #
    # Dockerfile example: <tt>RUN SECRET_KEY_BASE_DUMMY=1 bundle exec rails assets:precompile</tt>.
    #
    # In all other environments, we look for it first in <tt>ENV["SECRET_KEY_BASE"]</tt>,
    # then +credentials.secret_key_base+, and finally +secrets.secret_key_base+. For most applications,
    # the correct place to store it is in the encrypted credentials file.
    def secret_key_base
      if Rails.env.local? || ENV["SECRET_KEY_BASE_DUMMY"]
        config.secret_key_base ||= generate_local_secret
      else
        validate_secret_key_base(
          ENV["SECRET_KEY_BASE"] || credentials.secret_key_base || begin
            secret_skb = secrets_secret_key_base

            if secret_skb.equal?(config.secret_key_base)
              config.secret_key_base
            else
              Rails.deprecator.warn(<<~MSG.squish)
                Your `secret_key_base` is configured in `Rails.application.secrets`,
                which is deprecated in favor of `Rails.application.credentials` and
                will be removed in Rails 7.2.
              MSG

              secret_skb
            end
          end
        )
      end
    end

    # Returns an ActiveSupport::EncryptedConfiguration instance for the
    # credentials file specified by +config.credentials.content_path+.
    #
    # By default, +config.credentials.content_path+ will point to either
    # <tt>config/credentials/#{environment}.yml.enc</tt> for the current
    # environment (for example, +config/credentials/production.yml.enc+ for the
    # +production+ environment), or +config/credentials.yml.enc+ if that file
    # does not exist.
    #
    # The encryption key is taken from either <tt>ENV["RAILS_MASTER_KEY"]</tt>,
    # or from the file specified by +config.credentials.key_path+. By default,
    # +config.credentials.key_path+ will point to either
    # <tt>config/credentials/#{environment}.key</tt> for the current
    # environment, or +config/master.key+ if that file does not exist.
    def credentials
      @credentials ||= encrypted(config.credentials.content_path, key_path: config.credentials.key_path)
    end

    # Returns an ActiveSupport::EncryptedConfiguration instance for an encrypted
    # file. By default, the encryption key is taken from either
    # <tt>ENV["RAILS_MASTER_KEY"]</tt>, or from the +config/master.key+ file.
    #
    #   my_config = Rails.application.encrypted("config/my_config.enc")
    #
    #   my_config.read
    #   # => "foo:\n  bar: 123\n"
    #
    #   my_config.foo.bar
    #   # => 123
    #
    # Encrypted files can be edited with the <tt>bin/rails encrypted:edit</tt>
    # command. (See the output of <tt>bin/rails encrypted:edit --help</tt> for
    # more information.)
    def encrypted(path, key_path: "config/master.key", env_key: "RAILS_MASTER_KEY")
      ActiveSupport::EncryptedConfiguration.new(
        config_path: Rails.root.join(path),
        key_path: Rails.root.join(key_path),
        env_key: env_key,
        raise_if_missing_key: config.require_master_key
      )
    end

    def to_app # :nodoc:
      self
    end

    def helpers_paths # :nodoc:
      config.helpers_paths
    end

    console do
      unless ::Kernel.private_method_defined?(:y)
        require "psych/y"
      end
    end

    # Return an array of railties respecting the order they're loaded
    # and the order specified by the +railties_order+ config.
    #
    # While running initializers we need engines in reverse order here when
    # copying migrations from railties ; we need them in the order given by
    # +railties_order+.
    def migration_railties # :nodoc:
      ordered_railties.flatten - [self]
    end

    def load_generators(app = self) # :nodoc:
      app.ensure_generator_templates_added
      super
    end

    # Eager loads the application code.
    def eager_load!
      Rails.autoloaders.each(&:eager_load)
    end

  protected
    alias :build_middleware_stack :app

    def run_tasks_blocks(app) # :nodoc:
      railties.each { |r| r.run_tasks_blocks(app) }
      super
      load "rails/tasks.rb"
      task :environment do
        ActiveSupport.on_load(:before_initialize) { config.eager_load = config.rake_eager_load }

        require_environment!
      end
    end

    def run_generators_blocks(app) # :nodoc:
      railties.each { |r| r.run_generators_blocks(app) }
      super
    end

    def run_runner_blocks(app) # :nodoc:
      railties.each { |r| r.run_runner_blocks(app) }
      super
    end

    def run_console_blocks(app) # :nodoc:
      railties.each { |r| r.run_console_blocks(app) }
      super
    end

    def run_server_blocks(app) # :nodoc:
      railties.each { |r| r.run_server_blocks(app) }
      super
    end

    # Returns the ordered railties for this application considering railties_order.
    def ordered_railties # :nodoc:
      @ordered_railties ||= begin
        order = config.railties_order.map do |railtie|
          if railtie == :main_app
            self
          elsif railtie.respond_to?(:instance)
            railtie.instance
          else
            railtie
          end
        end

        all = (railties - order)
        all.push(self)   unless (all + order).include?(self)
        order.push(:all) unless order.include?(:all)

        index = order.index(:all)
        order[index] = all
        order
      end
    end

    def railties_initializers(current) # :nodoc:
      initializers = []
      ordered_railties.reverse.flatten.each do |r|
        if r == self
          initializers += current
        else
          initializers += r.initializers
        end
      end
      initializers
    end

    def default_middleware_stack # :nodoc:
      default_stack = DefaultMiddlewareStack.new(self, config, paths)
      default_stack.build_stack
    end

    def validate_secret_key_base(secret_key_base)
      if secret_key_base.is_a?(String) && secret_key_base.present?
        secret_key_base
      elsif secret_key_base
        raise ArgumentError, "`secret_key_base` for #{Rails.env} environment must be a type of String`"
      else
        raise ArgumentError, "Missing `secret_key_base` for '#{Rails.env}' environment, set this string with `bin/rails credentials:edit`"
      end
    end

    def ensure_generator_templates_added
      configured_paths = config.generators.templates
      configured_paths.unshift(*(paths["lib/templates"].existent - configured_paths))
    end

    private
      def generate_local_secret
        if config.secret_key_base.nil?
          key_file = Rails.root.join("tmp/local_secret.txt")

          if File.exist?(key_file)
            config.secret_key_base = File.binread(key_file)
          elsif secrets_secret_key_base
            config.secret_key_base = secrets_secret_key_base
          else
            random_key = SecureRandom.hex(64)
            FileUtils.mkdir_p(key_file.dirname)
            File.binwrite(key_file, random_key)
            config.secret_key_base = File.binread(key_file)
          end
        end

        config.secret_key_base
      end

      def secrets_secret_key_base
        Rails.deprecator.silence do
          secrets.secret_key_base
        end
      end

      def build_request(env)
        req = super
        env["ORIGINAL_FULLPATH"] = req.fullpath
        env["ORIGINAL_SCRIPT_NAME"] = req.script_name
        req
      end

      def build_middleware
        config.app_middleware + super
      end

      def coerce_same_site_protection(protection)
        protection.respond_to?(:call) ? protection : proc { protection }
      end

      def filter_parameters
        if config.precompile_filter_parameters
          config.filter_parameters.replace(
            ActiveSupport::ParameterFilter.precompile_filters(config.filter_parameters)
          )
        end
        config.filter_parameters
      end
  end
end
